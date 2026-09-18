"""规则匹配引擎（P2 §7 一期 + P4 语义融合）。

四分项规则分：
    rule = w_skill*skill_hit + w_city*city_fit + w_exp*exp_fit + w_role*role_fit
融合分（§7 二期阉割版，γ*llm 留位）：
    final = alpha * rule + beta * vec    # vec = TF-IDF 余弦（semantic.py）

- skill_hit：简历技能 ∩ 职位技能 / 职位技能；两侧均为 skill_tags 标准标签
  （简历解析时已归一，见 parse.py）。任一侧为空 → 0.5 中性（无法判断不惩罚）；
- exp_fit：job 无经验要求 → 1；resume 无年限 → 0.5；years >= min → 1；
  差 1~2 年 → 0.5；再低 → 0；
- city_fit：复用 §city 规范化的变体匹配（杭州 ⊂ 杭州市、多城市串、别名）；
  命中 → 1；职位支持远程 → 0.8；职位城市未知 → 0.5 中性；否则 0；
- role_fit：target_role 去修饰词（高级/资深/senior…）后与职位 title contains；
  命中 → 1；resume 无 target_role → 0.5 中性；否则 0。

结果写入 match_scores（uq resume_id+job_id，先删后插幂等）：
rule_score=四分项规则分，vec_score=文本相似度，final_score=融合分（排序用）。
profile 人工修正后由 API 层触发重算（§12.3 挂账销项）。
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Job, MatchScore, Resume
from app.pipelines.semantic import TfidfIndex, job_text, resume_query_text
from app.services.city import city_match_variants

# target_role 修饰词剥离（不参与 title 匹配）
_ROLE_MODIFIERS = ["资深", "高级", "中级", "初级", "专家", "senior", "junior", "expert", "lead"]
# 通用职级/职能后缀：剥掉后取核心词（"后端工程师"→"后端"，才能命中"后端开发工程师"）
_ROLE_GENERIC_SUFFIXES = ["工程师", "开发", "专员", "经理", "设计师", "顾问", "师"]
# 核心词同义词（实测库内"服务端"34 条 vs "后端"1 条，必须归并才算得动）
_ROLE_SYNONYMS: dict[str, list[str]] = {
    "后端": ["服务端", "backend", "server"],
    "前端": ["frontend", "web"],
}


def _skill_hit(profile: dict, job: Job, explain: list[dict]) -> float:
    resume_skills = {str(s).lower() for s in (profile.get("skills") or [])}
    job_skills = {str(s).lower() for s in (job.skills or [])}
    if not resume_skills or not job_skills:
        explain.append({"key": "skill", "score": 0.5, "note": "skills_unknown_neutral"})
        return 0.5
    hit = sorted(resume_skills & job_skills)
    score = len(hit) / len(job_skills)
    explain.append({"key": "skill", "score": round(score, 4), "hit": hit, "missing": sorted(job_skills - resume_skills)})
    return score


def _exp_fit(profile: dict, job: Job, explain: list[dict]) -> float:
    min_exp = job.experience_min
    years = profile.get("experience_years")
    if min_exp is None:
        score = 1.0
        note = "job_no_requirement"
    elif years is None:
        score = 0.5
        note = "resume_no_years"
    elif years >= min_exp:
        score = 1.0
        note = None
    elif years >= min_exp - 2:
        score = 0.5
        note = "slightly_below"
    else:
        score = 0.0
        note = "below"
    entry: dict = {"key": "exp", "score": score, "job_min": min_exp, "resume_years": years}
    if note:
        entry["note"] = note
    explain.append(entry)
    return score


def _city_fit(profile: dict, job: Job, explain: list[dict]) -> float:
    job_city = (job.city or "").strip()
    job_city_l = job_city.lower()
    variants: list[str] = []
    for c in profile.get("cities") or []:
        variants.extend(city_match_variants(str(c)))
    score = 0.0
    note = None
    if variants and any(v in job_city_l for v in variants):
        score = 1.0
    elif "remote" in job_city_l or "远程" in job_city:
        score = 0.8
        note = "remote_ok"
    elif not job_city or job_city.upper() == "N/A":
        score = 0.5
        note = "city_unknown_neutral"
    explain.append({"key": "city", "score": score, "job_city": job_city or None, "resume_cities": profile.get("cities") or [], **({"note": note} if note else {})})
    return score


def _role_fit(profile: dict, job: Job, explain: list[dict]) -> float:
    role = str(profile.get("target_role") or "").strip()
    if not role:
        explain.append({"key": "role", "score": 0.5, "note": "no_target_role"})
        return 0.5
    core = role.lower()
    for m in _ROLE_MODIFIERS:
        core = core.replace(m.lower(), "").strip()
    # 剥通用后缀取核心词："后端工程师"→"后端开发工程师" 也能命中
    essential = core
    for suf in _ROLE_GENERIC_SUFFIXES:
        while essential.endswith(suf) and len(essential) > len(suf):
            essential = essential[: -len(suf)]
    title_l = (job.title or "").lower()
    candidates = [essential, core] + _ROLE_SYNONYMS.get(essential, [])
    hit = next((c for c in candidates if c and c in title_l), None)
    score = 1.0 if hit else 0.0
    explain.append(
        {"key": "role", "score": score, "target_role": role, "job_title": job.title, **({"core": hit} if hit else {})}
    )
    return score


def compute_match(profile: dict, job: Job, vec_score: float | None = None) -> dict:
    """算单个职位得分；vec_score 提供时做 rule/vec 融合。

    返回 {"score": 最终分, "rule": 规则分, "explain": [...]}。
    """
    explain: list[dict] = []
    parts = [
        (settings.match_w_skill, _skill_hit(profile, job, explain)),
        (settings.match_w_city, _city_fit(profile, job, explain)),
        (settings.match_w_exp, _exp_fit(profile, job, explain)),
        (settings.match_w_role, _role_fit(profile, job, explain)),
    ]
    rule = round(sum(w * s for w, s in parts), 4)
    if vec_score is None:
        return {"score": rule, "rule": rule, "explain": explain}

    explain.append({"key": "semantic", "score": vec_score, "note": "tfidf_cosine"})
    final = round(
        settings.match_alpha * rule + settings.match_beta * vec_score, 4
    )
    return {"score": final, "rule": rule, "explain": explain}


def refresh_matches(session: Session, resume: Resume) -> int:
    """重算该简历对全部 active 职位的融合分，先删后插（幂等）。

    TF-IDF 索引每次重建（3470 条毫秒级），切 PG/向量模型时只换此实现。
    """
    session.execute(delete(MatchScore).where(MatchScore.resume_id == resume.id))
    jobs = session.execute(select(Job).where(Job.status == "active")).scalars().all()
    profile = resume.profile or {}

    index = TfidfIndex().fit([job_text(j.title, j.description, j.skills) for j in jobs])
    query_vec = index.build_query(resume_query_text(profile, resume.raw_text))
    use_semantic = bool(query_vec)  # 简历无有效文本时退纯规则分

    rows = []
    for i, job in enumerate(jobs):
        sim = index.similarity(i, query_vec) if use_semantic else None
        result = compute_match(profile, job, vec_score=sim)
        rows.append(
            MatchScore(
                resume_id=resume.id,
                job_id=job.id,
                rule_score=result["rule"],
                vec_score=sim,
                final_score=result["score"],
                explain=result["explain"],
            )
        )
    session.add_all(rows)
    session.commit()
    return len(rows)
