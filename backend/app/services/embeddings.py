"""嵌入提供者（§7 二期）：多语向量模型接入层，默认关闭。

设计口径（与 §7 一致）：
- `settings.embeddings_provider == "none"`（默认）→ `get_provider()` 返回 None，
  语义分继续走 `pipelines/semantic.py` 的 TF-IDF（零依赖、离线可测，P4 一期行为不变）；
- 配成 `sentence_transformers` → 懒加载 `SentenceTransformer`（**不装 torch 不 import、
  不下载模型**；首次 embed 才加载）。依赖属生产可选：`pip install sentence-transformers`；
- 模型默认 BAAI/bge-m3（多语、1024 维、L2 归一化后余弦 = 点积，与 pgvector `<=>`
  直接对应）；小内存机器可换 `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
  （384 维、~470MB），`EMBEDDINGS_MODEL` + `EMBEDDINGS_DIM` 同步改即可（向量列不定维）。

诚实口径：provider 配了但依赖/模型不可用必须**启动即报错**（fail loud），
不允许静默退化——退化路径只能由 match.py 的显式分流决定（见 vector_mode()）。
"""

from __future__ import annotations

from typing import Protocol

from app.core.config import settings


class EmbeddingProvider(Protocol):
    """嵌入协议：批量文本 → L2 归一化向量（维度 = dimension）。"""

    dimension: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class SentenceTransformerProvider:
    """sentence-transformers 懒加载提供者（BGE-M3 / MiniLM 等本地模型）。"""

    def __init__(
        self,
        model_name: str,
        device: str,
        dimension: int,
        batch_size: int = 32,
        max_seq_length: int = 0,
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.dimension = dimension
        self.batch_size = batch_size
        self.max_seq_length = max_seq_length
        self._model = None  # 懒加载：构造不 import torch、不下载模型

    def _ensure_model(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as e:  # pragma: no cover - 环境缺依赖时
                raise ImportError(
                    "embeddings_provider=sentence_transformers 但未安装依赖；"
                    "生产安装：pip install sentence-transformers（含 torch，约 2GB）。"
                    "不需要向量模式时把 EMBEDDINGS_PROVIDER 置回 none。"
                ) from e
            self._model = SentenceTransformer(self.model_name, device=self.device)
            if self.max_seq_length:
                # 覆盖模型自带截断长度（MiniLM 多语默认 128，装不下「标题+技能+正文」）
                self._model.max_seq_length = self.max_seq_length
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._ensure_model()
        # 长度感知分批：切批前按文本长度排序，出批后按下标还原。sentence-transformers
        # 把一批 padding 到**批内最长**样本，而 JD 长度长尾严重（本库 4547 条里 334 条
        # 超 4000 字符、最长 10975），按输入顺序切批时一条长 JD 就能把整批 32 条拉到
        # 8000+ token——实测全库回填慢 8 倍（2.6s/条 vs 0.32s/条），且会拖慢请求路径的
        # 现场补嵌。排序后 padding 浪费降到批内方差级别，语义结果不变（padding 是
        # attention-masked 的，逐条独立编码）。
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        vecs: list[list[float]] = []
        for start in range(0, len(order), self.batch_size):
            idx = order[start : start + self.batch_size]
            arr = model.encode(
                [texts[i] for i in idx],
                normalize_embeddings=True,  # L2 归一化：余弦 = 点积，<=> 距离 = 1 - 点积
                show_progress_bar=False,
            )
            vecs.extend([float(x) for x in row] for row in arr)
        restored = [None] * len(texts)
        for pos, i in enumerate(order):
            restored[i] = vecs[pos]
        vecs = restored  # type: ignore[assignment]
        bad = next((i for i, v in enumerate(vecs) if len(v) != self.dimension), None)
        if bad is not None:
            raise ValueError(
                f"嵌入维度不符：模型 {self.model_name} 产出 {len(vecs[bad])} 维，"
                f"配置 EMBEDDINGS_DIM={self.dimension}。换模型时两者必须同步修改。"
            )
        return vecs


def get_provider() -> EmbeddingProvider | None:
    """按配置返回嵌入提供者；none（默认）→ None，走 TF-IDF 老路。"""
    name = (settings.embeddings_provider or "none").strip().lower()
    if name in ("", "none"):
        return None
    if name == "sentence_transformers":
        return SentenceTransformerProvider(
            model_name=settings.embeddings_model,
            device=settings.embeddings_device,
            dimension=settings.embeddings_dim,
            batch_size=settings.embeddings_batch_size,
            max_seq_length=settings.embeddings_max_seq_len,
        )
    raise ValueError(
        f"未知 EMBEDDINGS_PROVIDER={name!r}（可选：none / sentence_transformers）"
    )
