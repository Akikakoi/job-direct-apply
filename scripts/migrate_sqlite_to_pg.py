"""SQLite → PostgreSQL 一次性数据迁移（P4 生产化）。

前置：
    1. docker compose up -d（backend/ 下，db 用 pgvector/pgvector:pg15）
    2. backend/.env 切 DATABASE_URL=postgresql+psycopg2://jda:jda@localhost:5432/jda
    3. backend/ 下执行：
       ../.venv/Scripts/python.exe ../scripts/migrate_sqlite_to_pg.py

流程：PG 侧 create_all（模型同源）→ 启用 vector 扩展 → 按 ID 顺序逐表搬运
（保留主键，jobs/companies 外键关系不乱）→ 行数对账。
幂等：upsert（on conflict do nothing），可重复执行。
"""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.models import (  # noqa: E402
    Application,
    Company,
    FeedbackLog,
    FetchLog,
    Job,
    MatchScore,
    Resume,
    SkillTag,
    User,
)
from app.core.db import init_db  # noqa: E402

# 搬运顺序：先无外键依赖的表；jobs 依赖 companies，match_scores 依赖 resume+job
TABLES = [
    Company,
    SkillTag,
    User,
    Resume,
    Job,
    MatchScore,
    Application,
    FeedbackLog,
    FetchLog,
]


def main() -> int:
    if not settings.database_url.startswith("postgresql"):
        print(f"错误：当前 DATABASE_URL 不是 PG（{settings.database_url}）。请先在 .env 切换。")
        return 1

    pg_engine = create_engine(settings.database_url)
    sqlite_engine = create_engine("sqlite:///./dev.db")

    with pg_engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        print("pgvector 扩展就绪")

    init_db()  # PG 侧 create_all（models 同源）
    print("PG 建表完成")

    total_moved = 0
    with Session(pg_engine) as dst, Session(sqlite_engine) as src:
        for model in TABLES:
            rows = src.query(model).all()
            moved = 0
            for row in rows:
                data = {
                    c.name: getattr(row, c.name)
                    for c in model.__table__.columns
                }
                # merge 语义：同主键已存在则跳过（幂等可重跑）
                pk = {c.name: data[c.name] for c in model.__table__.primary_key.columns}
                exists = dst.query(model).filter_by(**pk).first()
                if exists is None:
                    dst.add(model(**data))
                    moved += 1
            dst.commit()
            total_moved += moved
            print(f"{model.__tablename__:16s} {len(rows):5d} 行（新搬 {moved}）")

        # 重置 identity 序列到 max(id)（关键：搬运保留了显式主键，PG 的
        # identity sequence 仍从 1 计数，不重置则新插入撞主键 UniqueViolation）
        from sqlalchemy import create_engine as _ce, text as _text

        seq_engine = _ce(settings.database_url)
        with seq_engine.begin() as conn:
            for model in TABLES:
                table = model.__tablename__
                max_id = conn.execute(
                    _text(f"SELECT COALESCE(MAX(id), 0) FROM {table}")
                ).scalar_one()
                seq = conn.execute(
                    _text("SELECT pg_get_serial_sequence(:t, :c)"), {"t": table, "c": "id"}
                ).scalar()
                if seq is None:
                    continue
                conn.execute(
                    _text("SELECT setval(CAST(:s AS regclass), :v, :has)"),
                    {"s": seq, "v": max(max_id, 1), "has": max_id > 0},
                )
        print("\nidentity 序列已重置到 max(id)")

        # 对账
        print("\n行数对账（sqlite vs pg）：")
        ok = True
        for model in TABLES:
            n_src = src.query(model).count()
            n_dst = dst.query(model).count()
            flag = "OK" if n_src == n_dst else "MISMATCH"
            if n_src != n_dst:
                ok = False
            print(f"{model.__tablename__:16s} {n_src:5d} vs {n_dst:5d}  {flag}")
        print("\n迁移" + ("完成 ✅" if ok else "存在差异 ⚠️（可能含重复跳过）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
