"""规则匹配引擎（P2 §7 一期 + P4 语义融合）。

四分项规则分：
    rule = w_skill*skill_hit + w_city*city_fit + w_exp*exp_fit + w_role*role_fit
融合分（§7，γ*llm 留位）：
    final = alpha * rule + beta * vec
vec 分双路（build_vec_map 统一出口，match 与 tuning 同源）：
- 默认 TF-IDF 余弦（零依赖、离线可测，见 semantic.py）；
- §7 二期：EMBEDDINGS_PROVIDER 配置 + PGVector 就绪 → pgvector `<=>` 多语嵌入余弦
  （services/embeddings.py + vector_store.py），未就绪自动回退 TF-IDF。

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
    # 第二十五轮补齐：这三族在库内 title 里高频出现（"Software Engineer"、
    # "Platform Engineer"、"Infrastructure Engineer"），此前不在表内 → 中文
    # target_role（软件/平台/基础设施）对英文 title 恒 0。
    "软件": ["software"],
    "平台": ["platform"],
    "基础设施": ["infrastructure", "infra"],
}

# ---------- L3 职能族（第二十五轮）：把 role 分从 0/1 二值展开成梯度 ----------
# 动机（实测）：海外 1918 条里 role 是唯一真信号，二值化后"精确命中 1.0 / 其余
# 0.0"把绝大多数职位压成同一个分数——前 10 名门槛上并列近百条，真正决定出场
# 顺序的退化成 updated_at。分档后「同族其他职能 > 本族泛称 > 相邻族 > 未识别 >
# 明确非目标」可比，并列被打开。
#
# 档位（分值手拍，只保证**单调有序**：同一份简历下更相关的岗位分更高）：
#     1.00 同职能精确命中（核心词/同义词/中英互查，与旧行为一致，不引入语言偏置）
#     0.85 同族其他具体职能（后端简历看到 前端/移动/测试/运维/架构/安全）
#     0.60 本族泛称（software / platform / infrastructure / full stack）
#     0.40 相邻族（工程 ↔ 数据）
#     0.20 未识别（title 里没有任何已知职能词）
#     0.00 明确非目标（命中他族职能词或明确无关岗词表）
#
# 两个刻意的设计：
# - **未识别给 0.2 而不是 0**：英文 title 命名极其发散，"没见过"不等于"不相关"，
#   给 0 会把所有新命名一次性打死；
# - **同义词仍给 1.0**：把"同义词"降到 0.85 会让中文 target_role 配英文 title
#   系统性吃亏（正是 §12.7 #9 C 修掉的语言偏置），故不按"是否同义"分档。
_ROLE_FAMILY_MEMBERS: dict[str, list[str]] = {
    "engineering": ["后端", "前端", "全栈", "移动", "测试", "运维", "架构", "安全"],
    "data": ["数据", "算法"],
    "product": ["产品", "设计", "运营"],
    "business": ["销售", "财务", "人力"],
}

# 本族泛称：不特指某个职能、但明确属于该族的词。单独一档是因为
# "Software Engineer" 对任何工程职能都算匹配，却不该压过"后端"这种精确命中。
_ROLE_FAMILY_GENERIC: dict[str, list[str]] = {
    "engineering": ["software", "platform", "infrastructure", "infra", "systems", "sde", "engineer", "engineering"],
    "data": ["machine learning", "analytics", "artificial intelligence"],
    "product": ["growth"],
    "business": ["account manager", "accountant", "payroll", "accounts payable", "recruiter", "talent"],
}

# 相邻族：技能栈相邻的常见换岗路径（工程 ↔ 数据、产品 ↔ 运营）；其余跨族按
# "明确非目标"处理。
_ROLE_ADJACENT: dict[str, set[str]] = {
    "engineering": {"data"},
    "data": {"engineering"},
    "product": {"business"},
    "business": {"product"},
}

# 明确无关职能词：不属于上述任何族、但一眼看出与本产品用户群（技术求职者）
# 无关的门店/后勤/健身服务业岗。它们是"见过了、明确不要"，不是"命名没见过" → 0.0。
#
# 第二十八轮实测扩容：海外最大并列块（637 行）里 tier 全是 unrecognized_title，
# 内容 ~90% 是同一家公司（Equinox）的健身/门店岗——pilates instructors / style
# advisors / front desk associates / studio crew / massage therapists / lifeguards /
# estheticians / kids club associates…。旧词表只覆盖 15 个词且漏掉复数，等于把
# 这些岗和"engineering manager"这种真·泛称岗混在同一档 0.2 里。
# 只加"技术岗位标题里绝不会出现"的词；"associate/manager/lead/advisor" 这类
# 兼职级/职级语的**不进**——它们会误伤 "Associate Software Engineer"。
_ROLE_OFF_WORDS = (
    # 健身/运动/美容（Equinox 类）
    "trainer", "coach", "coaching", "instructor", "pilates", "yoga", "cycling",
    "massage", "therapist", "esthetician", "lifeguard", "salon", "spa", "wellness",
    "fitness", "gym", "studio", "crew", "locker", "membership",
    # 门店/前台/后勤
    "barista", "cashier", "stylist", "attendant", "janitor", "housekeeping", "valet",
    "desk", "receptionist", "concierge", "doorman", "bellman", "bartender",
    "dishwasher", "laundry", "maintenance", "cleaner", "nanny", "kids",
    # 其他明确无关专业
    "nurse", "driver", "cook", "chef", "culinary", "sommelier",
)

# 跨族同形词："operations" 同时挂在 运维（工程族）与 运营（产品族）下，参与族判定
# 会把 "Business Operations Manager" 判成工程岗。精确命中路径仍按 _ROLE_SYNONYMS
# 走（那边不受影响），只是不拿它判族。
_ROLE_FAMILY_AMBIGUOUS = {"operations"}

_ASCII_ONLY_RE = re.compile(r"[a-z0-9][a-z0-9 .,+#/&-]*")

# 标题主段切分（第二十八轮 L3 修正）：主职能几乎总在第一个分隔符之前，其后的片段
# 是领域/团队/地点修饰。族判定只看主段——实测 "Staff Data Scientist, Security"
# 因尾段 "Security" 被判工程族 family_peer 0.85，rule 冲到 0.925，把无关岗推进
# 入门简历的 top-10。只看主段后它正确落回相邻族 0.4。
# 刻意**不**按裸连字符切："Full-Stack Engineer" 会被切成 "Full"。
_TITLE_HEAD_SPLIT = re.compile(r"[,;:()\[\]|/，；：（）【】—–]|\s-\s")


def _title_head(title_l: str) -> str:
    """标题主段（第一个分隔符之前）；空串时退回整串，避免全分隔符标题判空。"""
    head = _TITLE_HEAD_SPLIT.split(title_l, maxsplit=1)[0].strip()
    return head or title_l


def _word_pattern(word: str) -> str:
    """ASCII 词按词边界匹配（"ui" 不能命中 "build"）；CJK 词直接用子串。

    英文**复数**必须认：不加 `s?` 时 "Licensed Massage Therapists"（19 条）、
    "Pilates Instructors"（62 条）都躲过 off-word 词表，落进 unrecognized 档——
    实测这是海外最大并列块（637 行）里的头两名来源。
    """
    if _ASCII_ONLY_RE.fullmatch(word):
        return rf"(?<![a-z0-9]){re.escape(word)}s?(?![a-z0-9])"
    return re.escape(word)


def _alternation(words) -> re.Pattern | None:
    pats = [
        _word_pattern(w.lower())
        for w in words
        if w and w.lower() not in _ROLE_FAMILY_AMBIGUOUS
    ]
    return re.compile("|".join(pats)) if pats else None


def _family_words(family: str) -> list[str]:
    """族的具体职能词 = 成员核心词 + 各自的同义词（英文写法一并算具体职能）。"""
    words: list[str] = []
    for member in _ROLE_FAMILY_MEMBERS[family]:
        words.append(member)
        words.extend(_ROLE_SYNONYMS.get(member, []))
    return words


_ROLE_FAMILY_PATTERNS: dict[str, re.Pattern] = {
    f: p for f in _ROLE_FAMILY_MEMBERS if (p := _alternation(_family_words(f))) is not None
}
_ROLE_GENERIC_PATTERNS: dict[str, re.Pattern] = {
    f: p for f in _ROLE_FAMILY_GENERIC if (p := _alternation(_ROLE_FAMILY_GENERIC[f])) is not None
}
_ROLE_OFF_PATTERN = _alternation(_ROLE_OFF_WORDS)


def _family_of_target(role_l: str) -> str | None:
    """target_role 所属族：先找具体职能词（取最先出现者），再退到泛称词。

    不只看剥完修饰/后缀的 `essential`——"AI 后端开发实习生"剥完仍是长串，
    但里面明明有"后端"，按整串查不到族会让 7/12 份简历直接掉回二值老路。
    """
    best: tuple[int, str] | None = None
    for family, pat in _ROLE_FAMILY_PATTERNS.items():
        m = pat.search(role_l)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), family)
    if best is not None:
        return best[1]
    for family, pat in _ROLE_GENERIC_PATTERNS.items():
        if pat.search(role_l):
            return family
    return None


def _other_family_hit(family: str, title_l: str) -> bool:
    """title 里是否出现了**非本族**的职能词（具体职能词或泛称词都算）。"""
    for other in _ROLE_FAMILY_MEMBERS:
        if other != family and _ROLE_FAMILY_PATTERNS[other].search(title_l):
            return True
    for other in _ROLE_FAMILY_GENERIC:
        if other != family and other in _ROLE_GENERIC_PATTERNS and _ROLE_GENERIC_PATTERNS[other].search(title_l):
            return True
    return False


def _adjacent_family_hit(family: str, title_l: str) -> bool:
    for other in _ROLE_ADJACENT.get(family, set()):
        if other in _ROLE_FAMILY_PATTERNS and _ROLE_FAMILY_PATTERNS[other].search(title_l):
            return True
        if other in _ROLE_GENERIC_PATTERNS and _ROLE_GENERIC_PATTERNS[other].search(title_l):
            return True
    return False


def _role_related(role_l: str, title_l: str, explain: list[dict], role: str, job: Job) -> float:
    """精确未命中时的分档（L3）。返回 0.85/0.6/0.4/0.2/0.0。

    族判定只用**标题主段**（`_title_head`）：尾段是领域/团队修饰词，不该把岗位
    "升级"成工程族（第二十八轮实测的 "Data Scientist, Security" 误判）。
    """
    family = _family_of_target(role_l)
    if family is None:
        # target_role 本身不在词表内 → 无从判断"相关"，不假装分档，保持二值老行为
        explain.append(
            {"key": "role", "score": 0.0, "target_role": role, "job_title": job.title, "tier": "unknown_target_role"}
        )
        return 0.0

    head = _title_head(title_l)
    tier = None
    if family in _ROLE_FAMILY_PATTERNS and _ROLE_FAMILY_PATTERNS[family].search(head):
        tier, score = "family_peer", 0.85
    elif _other_family_hit(family, head) and not _adjacent_family_hit(family, head):
        # 非相邻他族 + 明确无关词表 → 0；先于泛称档判断，避免 "Software Sales"
        # 因 "software" 被当成工程岗
        tier, score = "off_target", 0.0
    elif _ROLE_OFF_PATTERN is not None and _ROLE_OFF_PATTERN.search(head):
        tier, score = "off_target", 0.0
    elif _adjacent_family_hit(family, head):
        tier, score = "adjacent_family", 0.4
    elif family in _ROLE_GENERIC_PATTERNS and _ROLE_GENERIC_PATTERNS[family].search(head):
        tier, score = "family_generic", 0.6
    else:
        tier, score = "unrecognized_title", 0.2

    explain.append(
        {"key": "role", "score": score, "target_role": role, "job_title": job.title, "tier": tier, "family": family}
    )
    return score

# 区属判定（§12.7 #9 C）：与 main.DOMESTIC_SOURCES 保持一致的事实基础是
# "国内源城市全为中文、海外源城市全为非中文"（实测 2629 / 1918 无例外），
# 故按城市串里有无 CJK 判区，不依赖 PG 正则，SQLite 同样可用。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _skill_hit(profile: dict, job: Job, explain: list[dict], drop_unknown: bool = False) -> float | None:
    resume_skills = {str(s).lower() for s in (profile.get("skills") or [])}
    job_skills = {str(s).lower() for s in (job.skills or [])}
    if not resume_skills or not job_skills:
        explain.append({"key": "skill", "score": 0.5, "note": "skills_unknown_neutral"})
        # L1：任一侧没有技能标签 → 这一项**不可判定**。返回 None 让 weighted_rule
        # 把它的权重摊给其余项；否则恒 0.5 × 0.5 权重 = 0.25 常量，白占一半权重
        # 却只贡献"有没有标签"的偏置（海外场景实测的主要并列来源）。
        return None if drop_unknown else 0.5
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


def _city_fit(profile: dict, job: Job, explain: list[dict], drop_unknown: bool = False) -> float | None:
    job_city = (job.city or "").strip()
    job_city_l = job_city.lower()
    # 优先用规范化多城市索引串（city_keys，别名/多城市拆分已在采集侧展开）
    job_keys_l = (getattr(job, "city_keys", None) or "").lower()
    variants: list[str] = []
    for c in profile.get("cities") or []:
        variants.extend(city_match_variants(str(c)))
    score = 0.0
    note = None
    unknown = False
    if variants and (any(v in job_keys_l for v in variants if v) or any(v in job_city_l for v in variants)):
        score = 1.0
    elif "remote" in job_city_l or "远程" in job_city:
        score = 0.8
        note = "remote_ok"
    elif not job_city or job_city.upper() == "N/A":
        score = 0.5
        note = "city_unknown_neutral"
        unknown = True
    elif _cross_region(profile, job_city):
        # §12.7 #9 C：跨区不可比（简历只写了国内意向，不等于拒绝海外）→ 中性而非 0。
        # 同区不同城市（杭州 vs 乌鲁木齐）走到最后一行，仍是 0 明确不匹配。
        score = 0.5
        note = "cross_region_neutral"
        unknown = True
    explain.append({"key": "city", "score": score, "job_city": job_city or None, "resume_cities": profile.get("cities") or [], **({"note": note} if note else {})})
    # L1：城市未知/跨区不可比 → 这一项判不出来，交权重归一化（同 _skill_hit）
    return None if (unknown and drop_unknown) else score


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
    if hit:
        explain.append(
            {
                "key": "role",
                "score": 1.0,
                "target_role": role,
                "job_title": job.title,
                "core": hit,
                "tier": "exact",
            }
        )
        return 1.0
    # 精确未命中 → 走职能族分档（L3），不再一律 0
    return _role_related(role.lower(), title_l, explain, role, job)


def match_parts(
    profile: dict,
    job: Job,
    explain: list[dict] | None = None,
    drop_unknown: bool = False,
) -> dict[str, float]:
    """四分项**原始分**（不含权重）——加权求和与调参/看板共用的唯一打分源。

    `drop_unknown=True`（L1，仅 `WEIGHTS_AUTO_TUNE` 开时）：把"不可判定"的项
    （无技能标签 / 城市未知 / 跨区不可比）从结果里**剔除**而不是塞 0.5，由
    `weighted_rule` 把权重让给其余项；缺省 False = 全部四项齐全的旧行为。
    """
    expl: list[dict] = [] if explain is None else explain
    parts = {
        "skill": _skill_hit(profile, job, expl, drop_unknown),
        "city": _city_fit(profile, job, expl, drop_unknown),
        "exp": _exp_fit(profile, job, expl),
        "role": _role_fit(profile, job, expl),
    }
    return {k: v for k, v in parts.items() if v is not None}


def weighted_rule(
    parts: dict[str, float],
    w_skill: float,
    w_city: float,
    w_exp: float,
    w_role: float,
    normalize: bool = False,
) -> float:
    """按权重把四分项原始分合成规则分（调参网格搜索复用，保证与线上公式一致）。

    `normalize=True`（L2，仅 `WEIGHTS_AUTO_TUNE` 开时）：`parts` 里缺项时把缺项的
    权重**按比例摊给其余项**（不再让常量 0.5 占着权重把所有人压进窄区间）。
    必须与 L3 一起开——只归一分档不做，rule 会退化成 role 的线性函数，并列更多。
    """
    weights = {"skill": w_skill, "city": w_city, "exp": w_exp, "role": w_role}
    if normalize:
        kept = {k: w for k, w in weights.items() if k in parts}
        total = sum(kept.values())
        if total <= 0:
            return 0.0
        weights = {k: w / total for k, w in kept.items()}
    return round(sum(w * parts[k] for k, w in weights.items()), 4)


def compute_match(
    profile: dict,
    job: Job,
    vec_score: float | None = None,
    weights: "WeightSet | None" = None,
    vec_note: str = "tfidf_cosine",
) -> dict:
    """算单个职位得分；vec_score 提供时做 rule/vec 融合。

    `weights` 缺省用 `.env` 设置（旧行为）；线上重算由 `refresh_matches*` 传入**生效权重**
    （§12.5 反馈回灌闭环：可能是被采纳过的版本，见 `services/weights.py`）。
    `vec_note` 标注语义分来源（tfidf_cosine / embedding_cosine），进 explain 供审计。
    """
    from app.services.weights import WeightSet

    w = weights or WeightSet.from_settings()
    explain: list[dict] = []
    # L1+L2 权重归一：挂在 `WEIGHTS_AUTO_TUNE` 后面（默认关）。关 = 与旧口径逐位一致；
    # 开 = 不可判定项不再塞 0.5，权重按比例摊给其余项（口径变更，需全量重算 match_scores）。
    adaptive = settings.weights_auto_tune
    parts = match_parts(profile, job, explain, drop_unknown=adaptive)
    rule = weighted_rule(parts, w.skill, w.city, w.exp, w.role, normalize=adaptive)
    if vec_score is None:
        return {"score": rule, "rule": rule, "explain": explain}

    explain.append({"key": "semantic", "score": vec_score, "note": vec_note})
    final = round(w.alpha * rule + w.beta * vec_score, 4)
    return {"score": final, "rule": rule, "explain": explain}


class _LazyTfidf:
    """TF-IDF 索引惰性构建：向量模式下永不 fit，老路仍全量共享一次（4547 条毫秒级）。"""

    def __init__(self, jobs: list[Job]) -> None:
        self._jobs = jobs
        self._index: TfidfIndex | None = None

    def get(self) -> TfidfIndex:
        if self._index is None:
            self._index = TfidfIndex().fit(
                [job_text(j.title, j.description, j.skills) for j in self._jobs]
            )
        return self._index


def build_vec_map(
    session: Session,
    profile: dict,
    raw_text: str | None,
    jobs: list[Job],
    tfidf: _LazyTfidf | None = None,
) -> tuple[dict[int, float] | None, str]:
    """语义分统一出口（match 与 tuning 同源，防止调参口径漂移）。

    返回 (vec_map, note)：job_id → 余弦相似度；None 表示该简历无有效文本，退纯规则分。
    向量模式（provider 配置 + PG vector 就绪）→ pgvector `<=>`；否则 TF-IDF 老路。
    """
    from app.services import vector_store
    from app.services.embeddings import get_provider

    provider = get_provider()  # 配置错名 fail loud（ValueError 上抛）
    if provider is not None and vector_store.infra_ready(session):
        return _vec_map_embedding(session, profile, raw_text, jobs, provider), "embedding_cosine"

    index = (tfidf or _LazyTfidf(jobs)).get()
    query_vec = index.build_query(resume_query_text(profile, raw_text))
    if not query_vec:
        return None, "tfidf_cosine"
    return {job.id: index.similarity(i, query_vec) for i, job in enumerate(jobs)}, "tfidf_cosine"


def _vec_map_embedding(
    session: Session, profile: dict, raw_text: str | None, jobs: list[Job], provider
) -> dict[int, float] | None:
    """向量模式：简历查询文本一次嵌入，全库 active 职位 `<=>` 全量相似度。

    机器兜底：职位缺向量就现场补嵌（首次全库 4547 条约数分钟，生产先跑
    scripts/backfill_vectors.py 预热；此后仅新增职位增量嵌入）。
    """
    from app.services import vector_store

    qtext = resume_query_text(profile, raw_text)
    if not qtext:
        return None
    missing = vector_store.missing_vector_ids(session, [j.id for j in jobs])
    if missing:
        by_id = {j.id: j for j in jobs}
        texts = [
            job_text(by_id[jid].title, by_id[jid].description, by_id[jid].skills)
            for jid in missing
            if jid in by_id
        ]
        vecs = provider.embed(texts)
        vector_store.upsert_job_vectors(session, list(zip(missing, vecs)))
    qvec = provider.embed([qtext])[0]
    return dict(vector_store.search_similar(session, qvec))


def _rebuild(
    session: Session,
    resume: Resume,
    jobs: list[Job],
    vec_map: dict[int, float] | None,
    weights=None,
    vec_note: str = "tfidf_cosine",
) -> int:
    """对单份简历重建 match_scores（先删后插，幂等）；vec_map 与权重由调用方传入复用。"""
    if weights is None:
        from app.services.weights import active_weights

        weights = active_weights(session)
    session.execute(delete(MatchScore).where(MatchScore.resume_id == resume.id))
    profile = resume.profile or {}

    rows = []
    for job in jobs:
        sim = vec_map.get(job.id) if vec_map else None
        result = compute_match(profile, job, vec_score=sim, weights=weights, vec_note=vec_note)
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

    语义分经 `build_vec_map` 统一出口：provider+PGVector 就绪走向量，否则 TF-IDF。
    权重取**当前生效版本**（§12.5 闭环：可能来自被采纳的权重版本）。
    """
    from app.services.weights import active_weights

    jobs = session.execute(select(Job).where(Job.status == "active")).scalars().all()
    vec_map, vec_note = build_vec_map(
        session, resume.profile or {}, resume.raw_text, jobs
    )
    return _rebuild(
        session, resume, jobs, vec_map, weights=active_weights(session), vec_note=vec_note
    )


def refresh_matches_all(session: Session, resumes: list[Resume] | None = None) -> int:
    """全量简历重算（挂账销项：职位新增/更新/过期后由采集侧触发）。

    TF-IDF 索引惰性构建只一次，多份简历共享（4547 条职位 × N 份简历仍秒级）；
    向量模式下完全不 fit TF-IDF。返回重算的 match_scores 总行数；无简历时返回 0。
    """
    if resumes is None:
        resumes = session.execute(select(Resume)).scalars().all()
    if not resumes:
        return 0
    from app.services.weights import active_weights

    weights = active_weights(session)
    jobs = session.execute(select(Job).where(Job.status == "active")).scalars().all()
    tfidf = _LazyTfidf(jobs)
    total = 0
    for r in resumes:
        vec_map, vec_note = build_vec_map(session, r.profile or {}, r.raw_text, jobs, tfidf=tfidf)
        total += _rebuild(session, r, jobs, vec_map, weights=weights, vec_note=vec_note)
    return total
