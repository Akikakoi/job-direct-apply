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
  命中 → 1；职位支持远程 → 0.8；职位城市未知 → 0.5 中性；
  **跨区（国内城市简历 vs 海外职位，或反之）→ 0.5 中性**（§12.7 #9 C：
  简历里的城市列表只表达"国内意向"，对海外岗位属"不可比"，给 0 会系统性压低
  海外排序；同区内不同城市（杭州 vs 乌鲁木齐）仍是 0 明确不匹配）；否则 0；
- role_fit：target_role 去修饰词（高级/资深/senior…）后与职位 title contains；
  命中 → 1；resume 无 target_role → 0.5 中性；否则 0。
  候选词含**中英双向同义词表**（后端↔backend/server），解决中文简历 vs 英文
  title 的跨语言 role 分恒为 0（§12.7 #9 C）。

结果写入 match_scores（uq resume_id+job_id，先删后插幂等）：
rule_score=四分项规则分，vec_score=文本相似度，final_score=融合分（排序用）。
profile 人工修正后由 API 层触发重算（§12.3 挂账销项）。
"""

from __future__ import annotations

import re

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Job, MatchScore, Resume
from app.pipelines.semantic import TfidfIndex, job_text, resume_query_text
from app.services.city import city_match_variants, split_city_tokens

# target_role 修饰词剥离（不参与 title 匹配）
_ROLE_MODIFIERS = ["资深", "高级", "中级", "初级", "专家", "senior", "junior", "expert", "lead"]
# 通用职级/职能后缀：剥掉后取核心词（"后端工程师"→"后端"，才能命中"后端开发工程师"）
# ASCII 后缀同理由：英文 target_role "Backend Engineer" 也要能命中 "Backend Developer"
_ROLE_GENERIC_SUFFIXES = [
    "工程师", "开发", "专员", "经理", "设计师", "顾问", "师",
    "engineer", "developer", "designer", "consultant", "specialist", "manager",
]
# 核心词同义词（实测库内"服务端"34 条 vs "后端"1 条，必须归并才算得动）。
# §12.7 #9 C：中英双向——中文 target_role 能命中英文 title（后端 → backend），
# 英文 target_role 也能反查中文核心词命中中文 title（backend → 后端，见 _role_candidates）。
_ROLE_SYNONYMS: dict[str, list[str]] = {
    "后端": ["服务端", "backend", "back-end", "server"],
    "前端": ["frontend", "front-end", "web"],
    "全栈": ["fullstack", "full-stack"],
    "移动": ["mobile", "android", "ios"],
    "算法": ["algorithm", "machine learning"],
    "数据": ["data", "analytics"],
    "测试": ["qa", "testing"],
    "运维": ["devops", "sre", "operations"],
    "架构": ["architect", "architecture"],
    "安全": ["security"],
    "产品": ["product"],
    "运营": ["growth", "operations"],
    "设计": ["design", "ux", "ui"],
    "销售": ["sales", "account executive"],
    "财务": ["finance", "accounting"],
    "人力": ["human resources", "recruiting", "hr"],
}

# 区属判定（§12.7 #9 C）：与 main.DOMESTIC_SOURCES 保持一致的事实基础是
# "国内源城市全为中文、海外源城市全为非中文"（实测 2629 / 1918 无例外），
# 故按城市串里有无 CJK 判区，不依赖 PG 正则，SQLite 同样可用。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


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


def _city_region(city: str | None) -> str | None:
    """城市串的区属：cn（含中文城市）/ overseas（全非中文）；空串返回 None。"""
    tokens = [t for t in split_city_tokens(city) if t]
    if not tokens:
        return None
    return "cn" if any(_CJK_RE.search(t) for t in tokens) else "overseas"


def _cross_region(profile: dict, job_city: str) -> bool:
    """简历意向城市与职位城市是否分属国内/海外两区（任一侧无城市信息 → False）。"""
    job_region = _city_region(job_city)
    if job_region is None:
        return False
    regions = {_city_region(str(c)) for c in profile.get("cities") or []}
    regions.discard(None)
    return bool(regions) and job_region not in regions


def _city_fit(profile: dict, job: Job, explain: list[dict]) -> float:
    job_city = (job.city or "").strip()
    job_city_l = job_city.lower()
    # 优先用规范化多城市索引串（city_keys，别名/多城市拆分已在采集侧展开）
    job_keys_l = (getattr(job, "city_keys", None) or "").lower()
    variants: list[str] = []
    for c in profile.get("cities") or []:
        variants.extend(city_match_variants(str(c)))
    score = 0.0
    note = None
    if variants and (any(v in job_keys_l for v in variants if v) or any(v in job_city_l for v in variants)):
        score = 1.0
    elif "remote" in job_city_l or "远程" in job_city:
        score = 0.8
        note = "remote_ok"
    elif not job_city or job_city.upper() == "N/A":
        score = 0.5
        note = "city_unknown_neutral"
    elif _cross_region(profile, job_city):
        # §12.7 #9 C：跨区不可比（简历只写了国内意向，不等于拒绝海外）→ 中性而非 0。
        # 同区不同城市（杭州 vs 乌鲁木齐）走到最后一行，仍是 0 明确不匹配。
        score = 0.5
        note = "cross_region_neutral"
    explain.append({"key": "city", "score": score, "job_city": job_city or None, "resume_cities": profile.get("cities") or [], **({"note": note} if note else {})})
    return score


def _role_candidates(core: str, essential: str) -> list[str]:
    """role 候选词：核心词 + 中文核心词同义词/英文写法 + 英文核心词的**反向**中文核心词。

    双向是必要的：中文 target_role（后端）要配英文 title（Backend Engineer），
    英文 target_role（Backend Engineer）也要配中文 title（后端开发工程师）。
    """
    candidates = [essential, core]
    candidates.extend(_ROLE_SYNONYMS.get(essential, []))
    for zh, syns in _ROLE_SYNONYMS.items():
        if essential in syns or core in syns:
            candidates.append(zh)
    return candidates


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
            # 剥 ASCII 后缀会留下空格（"backend engineer" → "backend "），必须 strip
            essential = essential[: -len(suf)].strip()
    title_l = (job.title or "").lower()
    candidates = _role_candidates(core, essential)
    hit = next((c for c in candidates if c and c in title_l), None)
    score = 1.0 if hit else 0.0
    explain.append(
        {"key": "role", "score": score, "target_role": role, "job_title": job.title, **({"core": hit} if hit else {})}
    )
    return score


def match_parts(profile: dict, job: Job, explain: list[dict] | None = None) -> dict[str, float]:
    """四分项**原始分**（不含权重）——加权求和与调参/看板共用的唯一打分源。"""
    expl: list[dict] = [] if explain is None else explain
    return {
        "skill": _skill_hit(profile, job, expl),
        "city": _city_fit(profile, job, expl),
        "exp": _exp_fit(profile, job, expl),
        "role": _role_fit(profile, job, expl),
    }


def weighted_rule(parts: dict[str, float], w_skill: float, w_city: float, w_exp: float, w_role: float) -> float:
    """按权重把四分项原始分合成规则分（调参网格搜索复用，保证与线上公式一致）。"""
    return round(
        w_skill * parts["skill"] + w_city * parts["city"] + w_exp * parts["exp"] + w_role * parts["role"],
        4,
    )


def compute_match(
    profile: dict, job: Job, vec_score: float | None = None, weights: "WeightSet | None" = None
) -> dict:
    """算单个职位得分；vec_score 提供时做 rule/vec 融合。

    `weights` 缺省用 `.env` 设置（旧行为）；线上重算由 `refresh_matches*` 传入**生效权重**
    （§12.5 反馈回灌闭环：可能是被采纳过的版本，见 `services/weights.py`）。
    """
    from app.services.weights import WeightSet

    w = weights or WeightSet.from_settings()
    explain: list[dict] = []
    parts = match_parts(profile, job, explain)
    rule = weighted_rule(parts, w.skill, w.city, w.exp, w.role)
    if vec_score is None:
        return {"score": rule, "rule": rule, "explain": explain}

    explain.append({"key": "semantic", "score": vec_score, "note": "tfidf_cosine"})
    final = round(w.alpha * rule + w.beta * vec_score, 4)
    return {"score": final, "rule": rule, "explain": explain}


def _rebuild(session: Session, resume: Resume, jobs: list[Job], index: TfidfIndex, weights=None) -> int:
    """对单份简历重建 match_scores（先删后插，幂等）；索引与权重由调用方传入复用。"""
    if weights is None:
        from app.services.weights import active_weights

        weights = active_weights(session)
    session.execute(delete(MatchScore).where(MatchScore.resume_id == resume.id))
    profile = resume.profile or {}

    query_vec = index.build_query(resume_query_text(profile, resume.raw_text))
    use_semantic = bool(query_vec)  # 简历无有效文本时退纯规则分

    rows = []
    for i, job in enumerate(jobs):
        sim = index.similarity(i, query_vec) if use_semantic else None
        result = compute_match(profile, job, vec_score=sim, weights=weights)
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


def refresh_matches(session: Session, resume: Resume) -> int:
    """重算该简历对全部 active 职位的融合分，先删后插（幂等）。

    TF-IDF 索引每次重建（3470 条毫秒级），切 PG/向量模型时只换此实现。
    权重取**当前生效版本**（§12.5 闭环：可能来自被采纳的权重版本）。
    """
    from app.services.weights import active_weights

    jobs = session.execute(select(Job).where(Job.status == "active")).scalars().all()
    index = TfidfIndex().fit([job_text(j.title, j.description, j.skills) for j in jobs])
    return _rebuild(session, resume, jobs, index, weights=active_weights(session))


def refresh_matches_all(session: Session, resumes: list[Resume] | None = None) -> int:
    """全量简历重算（挂账销项：职位新增/更新/过期后由采集侧触发）。

    TF-IDF 索引只建一次，多份简历共享（4547 条职位 × N 份简历仍秒级）。
    返回重算的 match_scores 总行数；无简历时返回 0。
    """
    if resumes is None:
        resumes = session.execute(select(Resume)).scalars().all()
    if not resumes:
        return 0
    from app.services.weights import active_weights

    weights = active_weights(session)
    jobs = session.execute(select(Job).where(Job.status == "active")).scalars().all()
    index = TfidfIndex().fit([job_text(j.title, j.description, j.skills) for j in jobs])
    return sum(_rebuild(session, r, jobs, index, weights=weights) for r in resumes)
