"""PostgreSQL → SQLite 一次性回迁（去 Docker 化，2026-10-19）。

背景：项目全面移除 Docker（docker-compose.yml 删除，PG/Redis 容器下线），
日常运行回归 SQLite 单文件。PG 侧数据（九月下旬~十月的多轮成果：权重归一、
0005 精度重算、SR 详情补齐等）比 dev.db 新得多，须全量搬回后再拆容器。

前置：
    1. PG 数据卷已用临时容器挂出（宿主 5432 被其他进程占用，故用 5433）：
       docker run -d --name jda-pg-tmp -p 5433:5432 \
         -v backend_pgdata:/var/lib/postgresql/data \
         -e POSTGRES_USER=jda -e POSTGRES_PASSWORD=jda -e POSTGRES_DB=jda \
         pgvector/pgvector:pg15
    2. backend/ 下先备份旧库：cp dev.db dev.db.bak-<日期>
    3. backend/ 下执行：
       ../.venv/Scripts/python.exe ../scripts/migrate_pg_to_sqlite.py

流程：SQLite drop_all + create_all（模型同源，即 alembic 0005 之后的 schema）
→ 按 ID 顺序逐表搬运（保留主键；SQLite 自增天然取 max+1，无需 setval）
→ 行数对账。迁移完成后执行 `alembic stamp head` 补迁移历史。
幂等：每次 drop 重建，可重复执行（源 PG 只读，不写任何数据）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.models import (  # noqa: E402
    Application,
    Company,
    FeedbackLog,
    FetchLog,
    Job,
    MatchScore,
    MatchWeightVersion,
    Resume,
    SkillTag,
    User,
)
from app.models import Base  # noqa: E402

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
    MatchWeightVersion,
]


def main() -> int:
    parser = argparse.ArgumentParser(description="PG → SQLite 回迁")
    parser.add_argument(
        "--pg-url",
        default="postgresql+psycopg2://jda:jda@localhost:5433/jda",
        help="源 PG 连接串（临时容器的宿主映射端口）",
    )
    parser.add_argument(
        "--sqlite-url", default="sqlite:///./dev.db", help="目标 SQLite 连接串"
    )
    args = parser.parse_args()

    pg_engine = create_engine(args.pg_url)
    sq_engine = create_engine(args.sqlite_url)

    # 源库存在性预检
    inspector = inspect(pg_engine)
    missing = [m.__tablename__ for m in TABLES if not inspector.has_table(m.__tablename__)]
    if missing:
        print(f"错误：PG 缺少表 {missing}，源库不完整，中止。")
        return 1

    # 目标库重建（drop_all 只清本项目模型管理的表，不动 sqlite 内部表）
    # 注意：不走 init_db()——它读 settings.database_url（.env），与脚本目标库无关
    Base.metadata.drop_all(sq_engine)
    Base.metadata.create_all(sq_engine)
    print("SQLite 重建表结构完成")

    total_moved = 0
    with Session(pg_engine) as src, Session(sq_engine) as dst:
        for model in TABLES:
            rows = src.query(model).order_by(model.id).all()
            moved = 0
            for row in rows:
                data = {c.name: getattr(row, c.name) for c in model.__table__.columns}
                dst.add(model(**data))
                moved += 1
                # 分批 flush 防 SQLite 变量上限（jobs.description 很长，999 变量上限按 500 切）
                if moved % 200 == 0:
                    dst.flush()
            dst.commit()
            total_moved += moved
            print(f"{model.__tablename__:20s} {len(rows):6d} 行")

        # 对账
        print("\n行数对账（pg vs sqlite）：")
        ok = True
        for model in TABLES:
            n_src = src.query(model).count()
            n_dst = dst.query(model).count()
            flag = "OK" if n_src == n_dst else "MISMATCH"
            if n_src != n_dst:
                ok = False
            print(f"{model.__tablename__:20s} {n_src:6d} vs {n_dst:6d}  {flag}")
        print("\n迁移" + ("完成 ✅（接着跑 alembic stamp head 补迁移历史）" if ok else "存在差异 ⚠️"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
