"""存量职位向量回填（§7 二期 PGVector）：批量嵌入 jobs 写入 jobs.vector。

前置（三缺一即报错退出，不做静默降级）：
1. DATABASE_URL 指向 PG 且 pgvector 扩展可用（列由本脚本幂等创建）；
2. EMBEDDINGS_PROVIDER=sentence_transformers；
3. 依赖已装：pip install sentence-transformers（含 torch）；模型首次运行自动下载
   （HF 需代理，或设 HF_ENDPOINT=https://hf-mirror.com 走国内镜像）。

用法（backend/ 下）：
    ../.venv/Scripts/python.exe ../scripts/backfill_vectors.py --dry-run  # 只报缺向量数
    ../.venv/Scripts/python.exe ../scripts/backfill_vectors.py            # 只补缺（幂等）
    ../.venv/Scripts/python.exe ../scripts/backfill_vectors.py --rebuild  # 清空重嵌（换模型后）

说明：refresh_matches* 的向量分支缺向量时也会现场补嵌（机器兜底），本脚本的价值是
把首次全库嵌入（BGE-M3 CPU 约 4547 条 × 数分钟）从请求路径挪到运维窗口提前完成。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from sqlalchemy import select, text  # noqa: E402

from app.core.db import SessionLocal  # noqa: E402
from app.models import Job  # noqa: E402
from app.pipelines.semantic import job_text  # noqa: E402
from app.services import vector_store  # noqa: E402
from app.services.embeddings import get_provider  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只统计缺向量数，不嵌入")
    parser.add_argument("--rebuild", action="store_true", help="清空全部向量重嵌（换模型后用）")
    parser.add_argument("--batch", type=int, default=64, help="每批嵌入条数（默认 64）")
    parser.add_argument("--limit", type=int, default=None, help="最多处理多少条（调试用）")
    args = parser.parse_args()

    with SessionLocal() as session:
        if not vector_store._is_pg(session):
            print("DATABASE_URL 不是 PostgreSQL：向量回填只在 PG 上有意义（先切 PG 再跑）")
            return 2
        provider = get_provider()
        if provider is None:
            print("EMBEDDINGS_PROVIDER 未启用（none）：不需要向量回填")
            return 2
        if args.rebuild:
            session.execute(text("UPDATE jobs SET vector = NULL"))
            session.commit()
            print("已清空全部职位向量（--rebuild）")
        vector_store.ensure_column(session)

        missing = vector_store.missing_vector_ids(session)
        total = session.execute(select(text("count(*)")).select_from(Job)).scalar()
        print(f"职位总数 {total}，缺向量 {len(missing)} 条，模型 {settings_model()} "
              f"(dim={provider.dimension}, device={provider.device})")
        if args.dry_run or not missing:
            return 0
        if args.limit:
            missing = missing[: args.limit]

        done = 0
        started = time.time()
        for i in range(0, len(missing), args.batch):
            batch_ids = missing[i : i + args.batch]
            jobs = session.execute(select(Job).where(Job.id.in_(batch_ids))).scalars().all()
            texts = [job_text(j.title, j.description, j.skills) for j in jobs]
            vecs = provider.embed(texts)
            vector_store.upsert_job_vectors(session, list(zip([j.id for j in jobs], vecs)))
            done += len(batch_ids)
            rate = done / max(time.time() - started, 1e-6)
            print(f"  {done}/{len(missing)}  ({rate:.0f} 条/s)", flush=True)
        print(f"回填完成：{done} 条，耗时 {time.time() - started:.0f}s")
    return 0


def settings_model() -> str:
    from app.core.config import settings

    return settings.embeddings_model


if __name__ == "__main__":
    raise SystemExit(main())
