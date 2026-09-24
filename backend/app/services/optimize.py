"""AI 简历优化（§12.6 P5 ③）：对照目标 JD 逐条差距 + 改写建议。

默认路径**完全离线可算**（规则推导，零 LLM、零新依赖），LLM 只是可选兜底：

- **逐条差距（gaps）**：把「简历画像 vs 目标 JD」拆成五个可比口径，只列**未被满足**
  的项，每项带 `kind` / `status` / `title` / `detail`（证据与口径）/ `advice`（改写建议）——
  1. `skill`：职位技能逐项比对（两侧均小写，与 `match._skill_hit` 同源），缺失项各列一条；
  2. `experience`：`experience_years` vs `experience_min`，**口径与 `match._exp_fit` 一致**
     （无要求 → 不算差距；差 1~2 年 → partial；差更多 → miss）；
  3. `education`：`edu_degree` 与 `degree_req` 的等级序（phd 4 > master 3 > bachelor 2 >
     associate 1），未知学历记 unknown，低于要求记 miss；`degree_req` 为 `na`/空则不算差距；
  4. `city`：复用 `city.city_match_variants` 变体匹配（与 `match._city_fit` 同源）；命中或
     支持远程不算差距；**跨区（国内意向 vs 海外职位）按 §12.7 #9 C 的中性口径跳过**，不误报；
  5. `keyword`：JD 正文/标题里的 ASCII 硬词（技术/工具名，长度 ≥3、去停用词）vs 简历原文
     覆盖率；只把「JD 高频但简历未出现」的词按词频列为提示——**只提示、不诱导堆砌**。
- **改写建议（`advice`）**：给"改哪里 / 怎么改"的可执行口径，**只教如何更好地呈现真实
  经历**（量化结果、STAR 结构、把与 JD 最相关的经历前置），**不生成任何虚假经历**。
- **LLM 兜底（默认关）**：`use_llm=True` 且 `llm_api_key` 已配置时，额外调一次 LLM 产出
  候选改写要点（复用 `llm_extract._build_client`，同一套 base_url/model/timeout 配置）；
  未配置 / 调用失败**都不抛异常**，只在 `llm.status` 里如实标注（`not_configured`/`failed`），
  规则结果照常返回——"开了开关但没配 key"不能变成一个 500。

**诚实口径（硬约束）**：`notes` 显式声明"差距与建议属规则比对的改进提示，不替用户编造经历"；
缺口技能给的是"若无相关经历就不要写进简历，先补基础并如实说明"的口径。关键词块同理，
是否真有相关经历由用户自行判断——堆砌无经历的关键词会在面试暴露。

**已收敛的边界**：差距比对 / 改写建议 / 关键词覆盖 / 接口全部离线可测。**剩余条件（非代码）**：
LLM 兜底的模型选择与配额属配置项（`llm_*`）；简历版本管理与 PDF 导出属前端工程。
"""

from __future__ import annotations

import json
import re
from collections import Counter

from app.core.config import settings
from app.models import Job, Resume
from app.pipelines.match import match_parts
from app.pipelines.semantic import job_text, resume_query_text
from app.services.city import city_match_variants

# 学历等级序（比大小用；与 rules.DEGREE_PATTERNS 的取值域一致）
_DEGREE_RANK = {"associate": 1, "bachelor": 2, "master": 3, "phd": 4}

# JD 硬关键词：以字母开头的 ASCII 词（长度 ≥3），能覆盖 Python/Kubernetes/SQL/CI 一类
# 工具与技术名。**不用 CJK 2-gram**：中文 n-gram 会产出"负责/要求/经验"这类噪音，
# 对"缺词提示"没有价值（技术栈几乎都是 ASCII）。
_ASCII_TERM = re.compile(r"[a-z][a-z0-9+#.]{2,}")
# 高频英文虚词/招聘话术词（对 ATS 关键词匹配无意义，去掉以降低噪音）
_KEYWORD_STOP = {
    "the", "and", "for", "with", "you", "your", "our", "are", "will", "that", "this",
    "have", "has", "use", "using", "work", "working", "team", "teams", "role", "job",
    "able", "also", "from", "more", "who", "all", "any", "can", "new", "their", "them",
    "they", "not", "but", "its", "out", "own", "get", "per", "via", "etc", "must",
    "should", "may", "one", "two", "other", "into", "over", "year", "years", "within",
    "while", "well", "like", "such", "when", "what", "which", "how", "about", "these",
    "those", "including", "strong", "good", "excellent", "knowledge", "ability",
    "skills", "skill", "required", "require", "requires", "requirements", "preferred",
    "responsibilities", "plus", "level", "senior", "junior", "lead", "based", "across",
    "experience", "experienced", "familiar", "familiarity", "understanding", "etc", "join",
}

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

OPTIMIZE_SYSTEM_PROMPT = """你是资深简历顾问。基于候选人画像与目标职位 JD，给出**针对该 JD 的简历改写要点**，只输出 JSON：
summary: string 一句话总评（≤80 字）
suggestions: string[] 3~6 条可执行的改写建议，每条聚焦"改哪里 + 怎么改"。
硬约束：必须基于候选人**已有**的经历与技能，禁止编造未提及的经历、技能或数字；对缺口给"如实呈现 + 如何补强"的口径，不得教用户谎报。"""


def degree_rank(degree: str | None) -> int | None:
    """学历等级序（phd 4 > master 3 > bachelor 2 > associate 1）；未知 → None。"""
    return _DEGREE_RANK.get(str(degree).strip().lower()) if degree else None


def _ascii_terms(text: str) -> Counter:
    """ASCII 硬关键词词频（小写、去停用词）。"""
    return Counter(w for w in _ASCII_TERM.findall((text or "").lower()) if w not in _KEYWORD_STOP)


def keyword_coverage(
    profile: dict, job: Job, raw_text: str | None = None, top: int = 15
) -> dict:
    """JD 硬关键词对简历的覆盖率 + 未覆盖词（按 JD 词频降序取前 top 个）。"""
    jd = _ascii_terms(job_text(job.title, job.description, job.skills))
    if not jd:
        return {"jd_terms": 0, "covered": 0, "ratio": None, "missing": [], "note": "no_jd_terms"}
    resume_text = resume_query_text(profile, raw_text).lower()
    have = set(_ASCII_TERM.findall(resume_text))
    covered = sum(1 for t in jd if t in have)
    missing = [t for t, _ in jd.most_common() if t not in have][:top]
    return {
        "jd_terms": len(jd),
        "covered": covered,
        "ratio": round(covered / len(jd), 4),
        "missing": missing,
    }


def _skill_gaps(profile: dict, job: Job) -> tuple[list[dict], list[str]]:
    """职位技能逐项比对：返回 (缺失技能差距项, 命中技能列表)。"""
    resume_skills = {str(s).lower() for s in (profile.get("skills") or [])}
    job_skills = [str(s) for s in (job.skills or [])]
    if not job_skills or not resume_skills:
        # 任一侧为空属"无法判断"（与 match 的中性口径同源），不列为差距
        return [], []
    matched = [s for s in job_skills if s.lower() in resume_skills]
    gaps = [
        {
            "kind": "skill",
            "status": "miss",
            "title": f"缺少职位要求的技能：{s}",
            "detail": f"职位技能「{s}」未在简历技能与项目技术栈中出现。",
            "advice": (
                f"若确有相关经历：在项目描述里补一句用 {s} 解决的具体问题（含结果数字）；"
                "若未接触过：**不要写进简历**——先补基础概念并在面试中如实说明正在学习，"
                "虚假技能会在技术追问与背调中暴露。"
            ),
        }
        for s in job_skills
        if s.lower() not in resume_skills
    ]
    return gaps, matched


def _experience_gap(profile: dict, job: Job) -> dict | None:
    """经验年限差距（口径与 match._exp_fit 一致）；无要求/已满足 → None。"""
    min_exp = job.experience_min
    if min_exp is None:
        return None
    years = profile.get("experience_years")
    if years is None:
        return {
            "kind": "experience",
            "status": "unknown",
            "title": f"简历未注明工作年限（职位要求 {min_exp} 年+）",
            "detail": "画像中无 experience_years，无法判断是否满足年限要求。",
            "advice": "在简历摘要里如实写出总年限与关键节点的起止时间，便于招聘方与 ATS 快速判断。",
        }
    if years >= min_exp:
        return None
    status = "partial" if years >= min_exp - 2 else "miss"
    return {
        "kind": "experience",
        "status": status,
        "title": f"工作年限低于要求（简历 {years} 年 vs 要求 {min_exp} 年+）",
        "detail": f"年限缺口约 {min_exp - years} 年（差 1~2 年记接近，更多记不足）。",
        "advice": (
            "不要夸大年限：用项目时间线如实呈现；把与岗位最相关的经历前置到摘要，"
            "突出可迁移的高相关度成果（同类技术栈 / 同类业务规模）以弥补年限差距。"
        ),
    }


def _education_gap(profile: dict, job: Job) -> dict | None:
    """学历差距；职位无学历要求（空/na）/已满足 → None。"""
    req = (job.degree_req or "").strip().lower()
    if req in ("", "na", "none"):
        return None
    req_rank = degree_rank(req)
    if req_rank is None:
        return None
    have = profile.get("edu_degree")
    have_rank = degree_rank(have)
    if have_rank is None:
        return {
            "kind": "education",
            "status": "unknown",
            "title": f"简历未注明学历（职位要求 {req}）",
            "detail": "画像中无 edu_degree，无法判断是否满足学历要求。",
            "advice": "在简历教育经历里如实写明院校 / 专业 / 学历与时间，缺失学历信息常被 ATS 直接筛掉。",
        }
    if have_rank >= req_rank:
        return None
    return {
        "kind": "education",
        "status": "miss",
        "title": f"学历低于职位要求（{have} vs {req}）",
        "detail": "学历是多数公司的硬门槛，通常无法靠简历改写绕过。",
        "advice": (
            "如实填写学历，不要粉饰；可把与岗位强相关的项目、开源作品、证书前置，"
            "并在投递时优先选择学历要求为「本科及以上/不限」的同类岗位以提高命中率。"
        ),
    }


def _city_region(city: str) -> str | None:
    """城市串区属：含中文 → cn，全非中文 → overseas；空 → None（同 match._city_region）。"""
    city = (city or "").strip()
    if not city:
        return None
    return "cn" if _CJK_RE.search(city) else "overseas"


def _city_gap(profile: dict, job: Job) -> dict | None:
    """城市差距：命中/远程/跨区中性/职位城市未知 → None（不误报）。"""
    cities = [str(c) for c in (profile.get("cities") or [])]
    job_city = (job.city or "").strip()
    if not cities:
        return {
            "kind": "city",
            "status": "unknown",
            "title": "简历未注明期望工作城市",
            "detail": "画像中无 cities，无法与职位城市比对。",
            "advice": "在简历里写明期望城市（可多选并标注是否接受远程/异地），减少无效沟通。",
        }
    if not job_city or job_city.upper() == "N/A":
        return None
    job_l = job_city.lower()
    keys_l = (getattr(job, "city_keys", None) or "").lower()
    variants: list[str] = []
    for c in cities:
        variants.extend(city_match_variants(c))
    if any(v and (v in keys_l or v in job_l) for v in variants):
        return None  # 命中
    if "remote" in job_l or "远程" in job_city:
        return None  # 支持远程，城市不成问题
    job_region = _city_region(job_city)
    resume_regions = {_city_region(c) for c in cities}
    resume_regions.discard(None)
    if job_region and resume_regions and job_region not in resume_regions:
        # §12.7 #9 C：跨区不可比（简历只写了国内意向 ≠ 拒绝海外），按中性口径跳过不误报
        return None
    return {
        "kind": "city",
        "status": "miss",
        "title": f"期望城市与职位城市不一致（{job_city}）",
        "detail": "简历意向城市与职位所在城市同区但不一致，属明确不匹配。",
        "advice": "若可接受该城市，在期望城市里补上并注明是否接受异地/远程；不接受则优先投递本地同类岗位。",
    }


def _keyword_gap(profile: dict, job: Job, raw_text: str | None) -> tuple[dict | None, dict]:
    """关键词覆盖差距；覆盖完整或 JD 无硬词 → (None, coverage)。"""
    coverage = keyword_coverage(profile, job, raw_text)
    missing = coverage["missing"]
    if not missing:
        return None, coverage
    return (
        {
            "kind": "keyword",
            "status": "partial",
            "title": f"JD 高频关键词未在简历出现：{'、'.join(missing[:8])}",
            "detail": f"JD 硬关键词 {coverage['jd_terms']} 个，简历覆盖 {coverage['covered']} 个"
            f"（覆盖率 {coverage['ratio']}）。",
            "advice": (
                "仅当确有相关经历时，把对应技术与成果自然写进项目描述（ATS 靠真实内容做关键词匹配）；"
                "堆砌无经历的关键词会在面试追问中暴露，反而扣分。"
            ),
        },
        coverage,
    )


def build_optimization(resume: Resume, job: Job, use_llm: bool = False) -> dict:
    """生成简历优化报告：逐条差距 + 改写建议 + 关键词覆盖 +（可选）LLM 要点。"""
    profile = dict(resume.profile or {})
    skill_gaps, matched_skills = _skill_gaps(profile, job)

    gaps: list[dict] = list(skill_gaps)
    for candidate in (
        _experience_gap(profile, job),
        _education_gap(profile, job),
        _city_gap(profile, job),
    ):
        if candidate is not None:
            gaps.append(candidate)
    keyword_gap, coverage = _keyword_gap(profile, job, resume.raw_text)
    if keyword_gap is not None:
        gaps.append(keyword_gap)

    notes = [
        "差距与建议由「简历画像 vs 目标 JD」规则比对得出，属改进提示，**不替用户编造任何经历**。",
        "关键词块只提示 JD 高频但简历未出现的词，是否确有相关经历由用户自行判断（堆砌无经历的关键词会在面试暴露）。",
    ]
    if not (job.description or "").strip():
        notes.append("该 JD 无正文（采集未含 description），关键词覆盖仅基于标题与技能标签。")
    if not (job.skills or []):
        notes.append("该职位无 skills 标签（词典未命中且 LLM 兜底未开启），技能差距项为空——建议先读 JD 原文自行归纳。")

    llm_block = _llm_block(profile, job, use_llm)
    if use_llm and llm_block["status"] == "not_configured":
        notes.append("已请求 LLM 兜底但 llm_api_key 未配置，本次仅返回规则结果。")
    elif use_llm and llm_block["status"] == "failed":
        notes.append("LLM 兜底调用失败，已回退为纯规则结果（见 llm.error）。")

    explain: list[dict] = []
    return {
        "resume": {
            "id": resume.id,
            "user_id": resume.user_id,
            "lang": resume.lang,
            "skills": profile.get("skills") or [],
            "experience_years": profile.get("experience_years"),
            "edu_degree": profile.get("edu_degree"),
            "target_role": profile.get("target_role"),
            "cities": profile.get("cities") or [],
        },
        "target": {
            "job_id": job.id,
            "title": job.title,
            "city": job.city,
            "source": job.source,
            "apply_url": job.apply_url,
            "skills": job.skills or [],
            "experience_min": job.experience_min,
            "degree_req": job.degree_req,
        },
        "score": match_parts(profile, job, explain),
        "matched_skills": matched_skills,
        "gaps": gaps,
        "keyword_coverage": coverage,
        "llm": llm_block,
        "notes": notes,
    }


def _call_llm(profile: dict, job: Job) -> dict:
    """调一次 LLM 产出改写要点（复用 llm_extract 的客户端配置）。"""
    from app.pipelines.llm_extract import _build_client

    payload = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": OPTIMIZE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "resume": {
                            "skills": profile.get("skills") or [],
                            "experience_years": profile.get("experience_years"),
                            "edu_degree": profile.get("edu_degree"),
                            "target_role": profile.get("target_role"),
                            "cities": profile.get("cities") or [],
                        },
                        "jd": {
                            "title": job.title,
                            "skills": job.skills or [],
                            "experience_min": job.experience_min,
                            "degree_req": job.degree_req,
                            "description": (job.description or "")[:4000],
                        },
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
    }
    with _build_client() as client:
        resp = client.post("/chat/completions", json=payload)
        resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("LLM 返回非 JSON 对象")
    return data


def _llm_block(profile: dict, job: Job, use_llm: bool) -> dict:
    """LLM 兜底块；默认关。未配置/失败都如实标注，绝不抛异常影响规则结果。"""
    if not use_llm:
        return {"requested": False, "status": "disabled", "summary": None, "suggestions": []}
    from app.pipelines.llm_extract import LLMNotConfigured

    try:
        data = _call_llm(profile, job)
    except LLMNotConfigured:
        return {"requested": True, "status": "not_configured", "summary": None, "suggestions": []}
    except Exception as exc:  # noqa: BLE001 —— 兜底：任何失败都降级为规则结果
        return {
            "requested": True,
            "status": "failed",
            "error": str(exc)[:200],
            "summary": None,
            "suggestions": [],
        }
    return {
        "requested": True,
        "status": "ok",
        "summary": data.get("summary"),
        "suggestions": [str(s) for s in (data.get("suggestions") or [])],
    }