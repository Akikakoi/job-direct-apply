"""一次性回填 jobs.city_keys（city 多值升级的存量数据迁移）。

用法（backend/ 下）：
    ../.venv/Scripts/python.exe scripts/backfill_city_keys.py              # 回填 .env 指向的库
    DATABASE_URL=sqlite:///./dev.db ../.venv/Scripts/python.exe scripts/...  # 指定 SQLite
幂等：city_keys 已正确的行不重复写。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.core.db import SessionLocal  # noqa: E402
from app.models import Job  # noqa: E402
from app.services.city import city_keys  # noqa: E402


def main() -> None:
    session = SessionLocal()
    try:
        # 确保列存在（create_all 不给已有表加列；SQLite/PG 均支持 ADD COLUMN）
        engine = session.get_bind()
        from sqlalchemy import inspect, text

        cols = [c["name"] for c in inspect(engine).get_columns("jobs")]
        if "city_keys" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE jobs ADD COLUMN city_keys TEXT"))
            print("column added: jobs.city_keys")

        rows = session.execute(select(Job)).scalars().all()
        changed = 0
        for job in rows:
            keys = city_keys(job.city)
            if job.city_keys != keys:
                job.city_keys = keys
                changed += 1
        session.commit()
        print(f"total={len(rows)} changed={changed}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
