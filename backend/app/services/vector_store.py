"""PGVector 向量存取（§7 二期）：raw SQL 读写 jobs.vector，不进 SQLAlchemy 模型。

为什么列不建模：
- 单测全量跑在 SQLite（零依赖离线可测），SQLite 没有 vector 类型；列一旦进模型，
  create_all/模型导入就会被 PG 方言污染。故 jobs.vector 只在 PG 上由本模块
  `ensure_column()` 幂等建列（`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`），
  读写全走 raw SQL（`CAST(:v AS vector)` 参数绑定，**不引 pgvector python 包**，
  向量就是文本协议 `[0.1,0.2,...]`）。
- 列**不定维**：pgvector 允许无维度 vector 列存取与 `<=>` 检索（仅建索引需定维；
  现库 4547 行全表距离计算毫秒级，量大再定维建 HNSW）。维度一致性由
  `EMBEDDINGS_DIM` 配置在读写入口强制校验——换嵌入模型只改配置，不动表结构。

就绪语义（三层，match.py 只认最后一层）：
- infra_ready：PG 方言 + vector 扩展 + jobs.vector 列；
- vector_mode：infra_ready **且** 嵌入 provider 已配置——这才是"语义分走向量"。
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings


def _is_pg(session: Session) -> bool:
    bind = session.get_bind()
    return bind.dialect.name == "postgresql"


def validate_vec(vec: list[float]) -> None:
    """维度门禁：任何写入/检索前强制对齐 EMBEDDINGS_DIM（错配置早暴露）。"""
    if not vec or len(vec) != settings.embeddings_dim:
        raise ValueError(
            f"向量维度 {len(vec) if vec else 0} != EMBEDDINGS_DIM={settings.embeddings_dim}"
        )


def to_pg_vector(vec: list[float]) -> str:
    """float 列表 → pgvector 文本协议（6 位小数足够，1024 维省一半绑定体积）。"""
    validate_vec(vec)
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def infra_ready(session: Session) -> bool:
    """PG + vector 扩展 + jobs.vector 列三者齐备才 True（SQLite 恒 False）。"""
    if not _is_pg(session):
        return False
    ext = session.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")).first()
    if not ext:
        return False
    col = session.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'jobs' AND column_name = 'vector'"
        )
    ).first()
    return bool(col)


def vector_mode(session: Session) -> bool:
    """语义分是否走向量：基础设施就绪 + provider 已配置（none 视为未启用）。"""
    if not infra_ready(session):
        return False
    from app.services.embeddings import get_provider

    return get_provider() is not None


def ensure_column(session: Session) -> bool:
    """幂等建扩展与列（PG only）；返回是否就绪。SQLite 直接 False 不抛错。"""
    if not _is_pg(session):
        return False
    session.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    session.execute(text("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS vector vector"))
    session.commit()
    return infra_ready(session)


def missing_vector_ids(session: Session, job_ids: list[int] | None = None) -> list[int]:
    """缺向量的职位 id（供回填兜底；只看传入集合，NULL 视为缺）。"""
    if not _is_pg(session):
        return []
    sql = "SELECT id FROM jobs WHERE vector IS NULL"
    params: dict = {}
    if job_ids is not None:
        if not job_ids:
            return []
        sql += " AND id = ANY(:ids)"
        params["ids"] = list(job_ids)
    return [int(r[0]) for r in session.execute(text(sql), params).all()]


def upsert_job_vectors(session: Session, pairs: list[tuple[int, list[float]]]) -> int:
    """批量写职位向量（幂等覆盖）；非 PG 返回 0。维度门禁逐条校验。"""
    if not pairs or not _is_pg(session):
        return 0
    params = [{"id": jid, "v": to_pg_vector(vec)} for jid, vec in pairs]
    session.execute(
        text("UPDATE jobs SET vector = CAST(:v AS vector) WHERE id = :id"),
        params,
    )
    session.commit()
    return len(params)


def search_similar(
    session: Session, query_vec: list[float], limit: int | None = None, only_active: bool = True
) -> list[tuple[int, float]]:
    """`<=>` 余弦检索：返回 [(job_id, similarity)]，similarity = 1 - 距离 ∈ [-1, 1]。

    limit=None 时全量（供 refresh_matches 全对全打分）；未嵌入职位（NULL）不参与。
    非 PG 返回 []（调用方分流兜底）。
    """
    if not _is_pg(session):
        return []
    q = to_pg_vector(query_vec)
    sql = "SELECT id, 1 - (vector <=> CAST(:q AS vector)) AS sim FROM jobs"
    if only_active:
        sql += " WHERE status = 'active' AND vector IS NOT NULL"
    else:
        sql += " WHERE vector IS NOT NULL"
    sql += " ORDER BY vector <=> CAST(:q AS vector)"
    params: dict = {"q": q}
    if limit is not None:
        sql += " LIMIT :limit"
        params["limit"] = limit
    return [(int(r[0]), round(float(r[1]), 4)) for r in session.execute(text(sql), params).all()]
