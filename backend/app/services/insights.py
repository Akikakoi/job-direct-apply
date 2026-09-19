"""质量基线报告（挂账销项）：feedback_log 回灌数据 → 命中率 / NDCG / 分数对照。

口径（保守、可复核）：
- 反馈覆盖：有 outcome 反馈的投递数 / 总投递数；
- 命中率：(interview + offer) / 有反馈投递数。no_feedback 视为无响应，
  与 rejected 一样计入分母（不美化）；
- 分数对照：各 outcome 组的 match_scores.final_score 均值。仅统计
  application.resume_id 非空且能关联到打分的投递（feedback_log 与 match_scores
  的闭环口径），验证"分高 → 结果好"是否成立，为权重回归调参提供依据；
- NDCG@K：以推荐序（final_score 降序）为系统排序，最新反馈为相关度
  （offer=3 > interview=2 > rejected/no_feedback=0），衡量"好职位是否排得靠前"。
  只统计有反馈的简历，无相关反馈（全 0）的跳过。
"""

from __future__ import annotations

from collections import Counter, defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Application, FeedbackLog, MatchScore, Resume

GRADED_RELEVANCE = {"offer": 3, "interview": 2, "rejected": 0, "no_feedback": 0}


def ndcg(relevances: list[float], k: int = 10) -> float:
    """NDCG@k：relevances 为系统排序下的相关度序列。"""
    if k <= 0 or not relevances:
        return 0.0
    dcg = sum(rel / (i + 2) ** 0.5 for i, rel in enumerate(relevances[:k]) if rel > 0)
    ideal = sorted(relevances, reverse=True)[:k]
    idcg = sum(rel / (i + 2) ** 0.5 for i, rel in enumerate(ideal) if rel > 0)
    return round(dcg / idcg, 4) if idcg > 0 else 0.0


def build_quality_report(session: Session, user_id: int | None = None, k: int = 10) -> dict:
    """汇总质量报告；user_id 限定单用户（默认全量）。"""
    apps_stmt = select(Application)
    if user_id is not None:
        apps_stmt = apps_stmt.where(Application.user_id == user_id)
    apps = session.execute(apps_stmt).scalars().all()

    # 每条投递取最新一条有 outcome 的反馈
    latest: dict[int, str] = {}
    for fb in session.execute(
        select(FeedbackLog).order_by(FeedbackLog.id.asc())
    ).scalars():
        if fb.outcome and fb.application_id is not None:
            latest[fb.application_id] = fb.outcome  # id 升序 → 后写覆盖

    outcome_counts = Counter(o for o in (latest.get(a.id) for a in apps) if o)
    responded = sum(outcome_counts.values())
    hits = outcome_counts.get("interview", 0) + outcome_counts.get("offer", 0)

    # (resume_id, job_id) → final_score，供分数对照与 NDCG
    ms_lookup: dict[tuple[int, int], float] = {}
    ms_by_resume: dict[int, list[tuple[int, float]]] = defaultdict(list)
    for ms in session.execute(select(MatchScore)).scalars():
        if ms.resume_id is None or ms.job_id is None or ms.final_score is None:
            continue
        ms_lookup[(ms.resume_id, ms.job_id)] = float(ms.final_score)
        ms_by_resume[ms.resume_id].append((ms.job_id, float(ms.final_score)))

    # 分数对照：outcome → [final_score]
    score_groups: dict[str, list[float]] = defaultdict(list)
    for a in apps:
        outcome = latest.get(a.id)
        if not outcome or a.resume_id is None or a.job_id is None:
            continue
        score = ms_lookup.get((a.resume_id, a.job_id))
        if score is not None:
            score_groups[outcome].append(score)

    # NDCG@k：按简历分组，推荐序 vs 反馈相关度
    ndcgs: list[float] = []
    resume_ids_with_fb = {a.resume_id for a in apps if a.resume_id is not None and latest.get(a.id)}
    for rid in resume_ids_with_fb:
        relevance: dict[int, int] = {}
        for a in apps:
            if a.resume_id != rid or a.job_id is None:
                continue
            outcome = latest.get(a.id)
            if outcome:
                rel = GRADED_RELEVANCE.get(outcome, 0)
                relevance[a.job_id] = max(relevance.get(a.job_id, 0), rel)
        ranked = sorted(ms_by_resume.get(rid, []), key=lambda x: x[1], reverse=True)[:k]
        gains = [relevance.get(job_id, 0) for job_id, _ in ranked]
        if any(gains):
            ndcgs.append(ndcg(gains, k))

    return {
        "total_applications": len(apps),
        "outcome_counts": dict(outcome_counts),
        "feedback_coverage": round(responded / len(apps), 4) if apps else None,
        "hit_rate": round(hits / responded, 4) if responded else None,
        "avg_final_score_by_outcome": {
            outcome: round(sum(scores) / len(scores), 4)
            for outcome, scores in sorted(score_groups.items())
            if scores
        },
        "ndcg_at_k": round(sum(ndcgs) / len(ndcgs), 4) if ndcgs else None,
        "ndcg_resumes": len(ndcgs),
        "k": k,
    }
