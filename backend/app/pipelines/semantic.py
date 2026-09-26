"""轻量语义匹配（P4 一期）：纯 Python TF-IDF 余弦相似，零外部依赖。

为什么不直接上 PGVector（开发文档 §7 二期）：
- 现状 SQLite + 3470 条职位，千级语料 TF-IDF 毫秒级；
- 不需要 docker PG / embedding 模型 / torch，离线可测；
- 中文不引分词器，用 CJK 2-gram + ASCII 单词混合 token（n-gram 是 ES
  同款思路），对"后端开发工程师 vs 服务端开发"这类字面重叠足够敏感。

跨语言（§12.7 #9 遗留 C）：字面 token 之外再叠一张**中英岗位术语桥**（`_CROSS_LINGUAL`），
把两侧对称投影到同一 token 空间，解决"中文简历 vs 英文 JD 语义分恒为 0"；零依赖、
可离线测，神经多语模型留给 §7 二期 PGVector。

切 PostgreSQL 后本模块接口不变，vector 计算可平滑替换为 BGE/PGVector。

融合公式（§7 二期阉割版，γ*llm 留位）：
    final = alpha * rule_score + beta * vec_score
"""

from __future__ import annotations

import math
import re
from collections import Counter

_CJK = re.compile(r"[\u4e00-\u9fff]")
_ASCII_WORD = re.compile(r"[a-z0-9+#.]{2,}")

# 跨语言术语桥（§12.7 #9 遗留 C）：中文简历 vs 英文 JD 的 TF-IDF 分近 0，因为
# 两侧字面无重叠——CJK 2-gram 命不中 ASCII 词。这里用一张零依赖的中英岗位术语
# 对照表把两侧**对称投影**到同一 token 空间：任一侧出现术语，就在 token 里补上
# 对侧写法，使"中文简历里的 后端/算法/数据"能命中 "backend/algorithm/data"。
#
# 取舍（显式记录）：**不引入神经多语模型**（BGE-M3 等向量模型）——本模块的立身
# 之本是"纯 Python、零依赖、离线可测"，神经多语模型是 §7 二期 PGVector 的升级
# 路径；此表只覆盖岗位大类与高频域名词，长尾技术术语靠技能标签兜底（职位/简历
# skills 已归一到同一套标准标签，本身即跨语言对齐的）。
# 注入词一律为 ASCII 单词（无空格/连字符），与 `_ASCII_WORD` 的 token 形状一致。
_CROSS_LINGUAL: dict[str, tuple[str, ...]] = {
    "后端": ("backend", "server"),
    "服务端": ("backend", "server"),
    "前端": ("frontend", "web"),
    "全栈": ("fullstack",),
    "移动端": ("mobile", "android", "ios"),
    "客户端": ("mobile", "client"),
    "算法": ("algorithm", "algorithms"),
    "机器学习": ("machine", "learning"),
    "深度学习": ("neural", "learning"),
    "数据": ("data", "analytics"),
    "测试": ("testing", "qa"),
    "运维": ("devops", "sre", "operations"),
    "架构": ("architecture", "architect"),
    "安全": ("security",),
    "产品": ("product",),
    "运营": ("operations", "growth"),
    "市场": ("marketing",),
    "销售": ("sales",),
    "财务": ("finance", "accounting"),
    "人力": ("human", "recruiting"),
    "法务": ("legal", "compliance"),
    "供应链": ("supply", "chain"),
    "游戏": ("game", "gaming"),
    "电商": ("ecommerce",),
    "金融": ("fintech",),
    "医疗": ("healthcare", "medical"),
}

# 职能/领域泛称投影（第二十八轮）：上表只覆盖"岗位大类"，实测海外 1920 条里
# 1533 条（79.8%）description + skills 双空，语义分只剩 5~9 个标题 token；而标题
# 的主流写法是「泛称职能 + 领域修饰」（"Staff Software Engineer, Payments
# Intelligence"）。泛称层没投影时，中文简历与这类标题零交集 → vec 恒 0 → 640~1560
# 行的并列块（**100% 海外、100% vec=0**）。
#
# 这里把泛称职能词与常见领域词也做成双向投影：英文标题里的 engineer/software/
# platform 补出中文核心词，中文简历里的"工程/软件/平台"补出英文写法。实测海外
# vec>0 占比 7.3% → 30.1%，最大并列块 725 → 621 行。
#
# 取舍：这些词**分辨力弱于技能术语**（"Engineer" 对任何工程岗都成立），只加"是否
# 沾边"的信号、不加"多相关"的信号——故不放进技能标签口径，只影响语义分。
_CROSS_LINGUAL_FUNCTION: dict[str, tuple[str, ...]] = {
    # 工程侧职能泛称
    "工程": ("engineer", "engineering"),
    "软件": ("software", "sde"),
    "平台": ("platform",),
    "系统": ("systems", "system"),
    "开发": ("developer", "development"),
    "基础设施": ("infrastructure", "infra"),
    # 其他族职能泛称
    "设计": ("design", "designer", "ux", "ui"),
    "经理": ("manager",),
    "总监": ("director",),
    "专员": ("specialist",),
    "分析": ("analyst", "analytics", "analysis"),
    # 领域/业务词（英文标题尾段与中文简历正文高频）
    "战略": ("strategy", "strategic"),
    "支持": ("support",),
    "风控": ("risk",),
    "合规": ("compliance",),
    "招聘": ("recruit", "recruiting", "hiring"),
    "客户": ("customer",),
    "合作伙伴": ("partner", "partners", "partnership", "partnerships"),
    "咨询": ("consulting", "consultant"),
    "支付": ("payments", "payment", "billing"),
    "保险": ("insurance",),
    "零售": ("retail",),
    "制造": ("manufacturing",),
    "物流": ("logistics",),
    "教育": ("education",),
}

# 词表合并视图：tokenize / 反向桥 / 正向桥统一从这里取，避免三处各写一遍
_CROSS_LINGUAL_ALL: dict[str, tuple[str, ...]] = {**_CROSS_LINGUAL, **_CROSS_LINGUAL_FUNCTION}


def _build_reverse_index() -> list[tuple[re.Pattern[str], str]]:
    """英文术语 → 中文核心词（反向桥）：同义词撞车时保留首个（如 server → 后端）。"""
    pairs: dict[str, str] = {}
    for zh, terms in _CROSS_LINGUAL_ALL.items():
        for en in terms:
            pairs.setdefault(en, zh)
    # 词边界匹配：避免 "web" 命中 "website"、"qa" 命中 "qatar" 一类子串误判
    return [
        (re.compile(rf"(?<![a-z0-9]){en}(?![a-z0-9])"), zh) for en, zh in pairs.items()
    ]


_REVERSE_BRIDGE = _build_reverse_index()


def cross_lingual_tokens(text: str) -> list[str]:
    """跨语言桥补出的附加 token：中文术语 → 补英文写法；英文术语 → 补中文核心词。"""
    if not text:
        return []
    lowered = text.lower()
    extra: list[str] = []
    for zh, terms in _CROSS_LINGUAL_ALL.items():
        if zh in text:
            extra.extend(terms)
    for pattern, zh in _REVERSE_BRIDGE:
        if pattern.search(lowered):
            extra.append(zh)
    return extra


def tokenize(text: str) -> list[str]:
    """混合 token：CJK 相邻 2-gram + ASCII 词（小写）+ 跨语言术语桥补词。"""
    text = (text or "").lower()
    tokens: list[str] = []
    for word in _ASCII_WORD.findall(text):
        tokens.append(word)
    # 提取 CJK 段做 2-gram（跨 ASCII 边界不连）
    cjk_runs = re.findall(r"[\u4e00-\u9fff]+", text)
    for run in cjk_runs:
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    tokens.extend(cross_lingual_tokens(text))
    return tokens


class TfidfIndex:
    """fit 一批文档后可 transform 查询文本并算余弦。"""

    def __init__(self) -> None:
        self.idf: dict[str, float] = {}
        self.vectors: list[dict[str, float]] = []  # L2 归一化后的 doc 向量
        self.n_docs = 0

    def fit(self, texts: list[str]) -> "TfidfIndex":
        df: Counter = Counter()
        docs_tokens = [tokenize(t) for t in texts]
        for toks in docs_tokens:
            df.update(set(toks))
        self.n_docs = len(docs_tokens)
        self.idf = {
            tok: math.log((self.n_docs + 1) / (cnt + 1)) + 1.0 for tok, cnt in df.items()
        }
        self.vectors = [self._vectorize_tokens(toks) for toks in docs_tokens]
        return self

    def _vectorize_tokens(self, tokens: list[str]) -> dict[str, float]:
        tf = Counter(tokens)
        vec = {tok: cnt * self.idf.get(tok, 1.0) for tok, cnt in tf.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {tok: v / norm for tok, v in vec.items()}

    def transform(self, text: str) -> dict[str, float]:
        return self._vectorize_tokens(tokenize(text))

    def similarity(self, doc_index: int, query_vec: dict[str, float]) -> float:
        doc = self.vectors[doc_index]
        return round(sum(w * doc.get(tok, 0.0) for tok, w in query_vec.items()), 4)

    def build_query(self, text: str) -> dict[str, float]:
        return self.transform(text)


def job_text(title: str, description: str | None, skills: list | None) -> str:
    return f"{title} {' '.join(skills or [])} {description or ''}"


def resume_query_text(profile: dict, raw_text: str | None = None) -> str:
    """简历侧查询文本：结构化字段优先（防 raw_text 噪音），原文兜底补充。"""
    parts = [
        " ".join(str(s) for s in (profile.get("skills") or [])),
        str(profile.get("target_role") or ""),
        " ".join(str(c) for c in (profile.get("cities") or [])),
    ]
    if raw_text:
        parts.append(raw_text[:3000])
    return " ".join(p for p in parts if p)
