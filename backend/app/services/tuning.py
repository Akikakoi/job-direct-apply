"""匹配权重回归调参（§12.7 #3）：用已回灌的反馈数据在权重网格上评估 NDCG@k。

口径与质量报告（insights.py）同源：
- 目标函数 = NDCG@k（推荐序 vs 反馈相关度 offer=3 > interview=2 > 0）；
- 候选 = 规则权重四维在 simplex（sum=1）上按 step 取网格 × α 候选（β=1−α）；
- 只统计"有非 0 相关度反馈"的简历，样本不足时不给结论（best=None）。

性能设计：四分项原始分（skill/city/exp/role）与 TF-IDF 语义分**都与权重无关**，
只算一次；此后每个候选组合只是纯算术加权 + 排序 —— 网格搜索不重复付打分成本。
线上公式与 `match.weighted_rule` 共用，避免调参与实际排序口径漂移。

注意：只读取数据、给出建议，**不写 .env、不改配置**；采纳需人工确认后改
MATCH_W_* / MATCH_ALPHA 并重算 match_scores（`refresh_matches_all` 或
`POST /api/resumes/{id}/profile`）。
"""

from __future__ import annotations

from itertools import product

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Application, Job, Resume
from app.pipelines.match import match_parts, weighted_rule
from app.pipelines.semantic import TfidfIndex, job_text, resume_query_text
from app.services.insights import GRADED_RELEVANCE, latest_outcomes, ndcg, weights_snapshot

WEIGHT_KEYS = ("skill", "city", "exp", "role")
DEFAULT_ALPHAS = (0.6, 0.75, 0.9)


def simplex_grid(step: float = 0.1) -> list[dict[str, float]]:
    """四维权重网格（每维为 step 的倍数且 sum=1）；step=0.1 → 286 组。"""
    n = round(1 / step)
    out: list[dict[str, float]] = []
    for combo in product(range(n + 1), repeat=len(WEIGHT_KEYS) - 1):
        rest = n - sum(combo)
        if rest < 0:
            continue
        values = [v * step for v in (*combo, rest)]
        out.append({k: round(v, 4) for k, v in zip(WEIGHT_KEYS, values)})
    return out


def collect_samples(session: Session) -> dict:
    """预计算调参样本：每份"有非 0 相关度反馈"的简历 → 全量 active 职位的 (parts, vec)。

    返回 {"resumes": [{"resume_id", "relevance": {job_id: rel}, "rows": [(job_id, parts, vec)]}],
          "jobs": n}；无可用样本时 resumes 为空列表。
    """
    latest = latest_outcomes(session)
    relevance: dict[int, dict[int, int]] = {}
    for app in session.execute(select(Application)).scalars():
        if app.resume_id is None or app.job_id is None:
            continue
        outcome = latest.get(app.id)
        if not outcome:
            continue
        graded = GRADED_RELEVANCE.get(outcome, 0)
        per_resume = relevance.setdefault(app.resume_id, {})
        per_resume[app.job_id] = max(per_resume.get(app.job_id, 0), graded)

    resume_ids = [rid for rid, rel in relevance.items() if any(v > 0 for v in rel.values())]
    if not resume_ids:
        return {"resumes": [], "jobs": 0}

    jobs = session.execute(select(Job).where(Job.status == "active")).scalars().all()
    index = TfidfIndex().fit([job_text(j.title, j.description, j.skills) for j in jobs])

    samples: list[dict] = []
    for rid in resume_ids:
        resume = session.get(Resume, rid)
        if resume is None:
            continue
        profile = resume.profile or {}
        query_vec = index.build_query(resume_query_text(profile, resume.raw_text))
        rows = [
            (job.id, match_parts(profile, job), index.similarity(i, query_vec) if query_vec else None)
            for i, job in enumerate(jobs)
        ]
        samples.append({"resume_id": rid, "relevance": relevance[rid], "rows": rows})
    return {"resumes": samples, "jobs": len(jobs)}


def ndcg_at_k(samples: list[dict], weights: dict[str, float], alpha: float, k: int) -> float | None:
    """候选权重下的平均 NDCG@k（无有效样本返回 None）。"""
    scores: list[float] = []
    beta = round(1 - alpha, 4)
    for sample in samples:
        ranked = sorted(
            sample["rows"],
            key=lambda row: _final(row, weights, alpha, beta),
            reverse=True,
        )[:k]
        gains = [sample["relevance"].get(job_id, 0) for job_id, _, _ in ranked]
        if any(gains):
            scores.append(ndcg(gains, k))
    return round(sum(scores) / len(scores), 4) if scores else None


def _final(row: tuple, weights: dict[str, float], alpha: float, beta: float) -> float:
    job_id, parts, vec = row
    rule = weighted_rule(
        parts, weights["skill"], weights["city"], weights["exp"], weights["role"]
    )
    return rule if vec is None else round(alpha * rule + beta * vec, 4)


def env_snippet(weights: dict[str, float]) -> str:
    """建议参数的 .env 片段（人工确认后再写入）。"""
    return (
        f"MATCH_W_SKILL={weights['skill']}\n"
        f"MATCH_W_CITY={weights['city']}\n"
        f"MATCH_W_EXP={weights['exp']}\n"
        f"MATCH_W_ROLE={weights['role']}\n"
        f"MATCH_ALPHA={weights['alpha']}"
    )


def search_weights(
    session: Session,
    k: int = 10,
    step: float = 0.1,
    alphas: tuple[float, ...] = DEFAULT_ALPHAS,
    top: int = 5,
    min_resumes: int = 3,
) -> dict:
    """在权重网格上搜索 NDCG@k 最优参数；样本不足只返回当前基线与提示。"""
    data = collect_samples(session)
    samples = data["resumes"]
    baseline_weights = weights_snapshot()
    baseline = {
        "weights": baseline_weights,
        "ndcg_at_k": ndcg_at_k(samples, baseline_weights, baseline_weights["alpha"], k),
    }
    report: dict = {
        "samples": {"resumes": len(samples), "jobs": data["jobs"]},
        "baseline": baseline,
        "best": None,
        "candidates": [],
        "grid": {"step": step, "combos": 0},
        "notes": [],
    }
    if len(samples) < min_resumes:
        report["notes"].append(
            f"有反馈（非 0 相关度）的简历仅 {len(samples)} 份 < {min_resumes}，"
            "结论不可靠，暂不建议调参；继续积累投递反馈后再跑。"
        )
        return report

    grid = simplex_grid(step)
    candidates: list[dict] = []
    for weights in grid:
        for alpha in alphas:
            score = ndcg_at_k(samples, weights, alpha, k)
            if score is None:
                continue
            candidates.append(
                {"weights": {**weights, "alpha": alpha, "beta": round(1 - alpha, 4)},
                 "ndcg_at_k": score}
            )
    candidates.sort(key=lambda c: (-c["ndcg_at_k"], c["weights"]["alpha"]))
    report["grid"]["combos"] = len(candidates)
    report["candidates"] = candidates[:top]

    best = candidates[0]
    report["best"] = best
    if baseline["ndcg_at_k"] is not None and best["ndcg_at_k"] > baseline["ndcg_at_k"]:
        report["best"]["gain"] = round(best["ndcg_at_k"] - baseline["ndcg_at_k"], 4)
        report["best"]["env_snippet"] = env_snippet(best["weights"])
    else:
        report["best"] = None
        report["notes"].append(
            "网格内未找到优于当前权重的组合（当前参数已是样本内最优），无需调整。"
        )
    if len(samples) < 10:
        report["notes"].append("样本量偏小（<10 份简历），建议仅作趋势参考。")
    return report