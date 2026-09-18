"""轻量语义匹配（P4 一期）：纯 Python TF-IDF 余弦相似，零外部依赖。

为什么不直接上 PGVector（开发文档 §7 二期）：
- 现状 SQLite + 3470 条职位，千级语料 TF-IDF 毫秒级；
- 不需要 docker PG / embedding 模型 / torch，离线可测；
- 中文不引分词器，用 CJK 2-gram + ASCII 单词混合 token（n-gram 是 ES
  同款思路），对"后端开发工程师 vs 服务端开发"这类字面重叠足够敏感。

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


def tokenize(text: str) -> list[str]:
    """混合 token：CJK 相邻 2-gram + ASCII 词（小写）。"""
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
