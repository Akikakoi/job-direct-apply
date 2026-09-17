"""采集 CLI（P1 以同步入口代替 Celery beat，调度接入在 P2+）。

用法：
    python -m app.workers.collect_cli init   # 建表
    python -m app.workers.collect_cli seed   # 写入首批公司与技能字典
    python -m app.workers.collect_cli list   # 查看映射表
    python -m app.workers.collect_cli run --company stripe
    python -m app.workers.collect_cli run --all
"""

from __future__ import annotations

import argparse

from sqlalchemy import select

from app.core.db import SessionLocal, init_db
from app.services.collect import collect_all, collect_company
from app.services.seed import run_seed


def main() -> int:
    parser = argparse.ArgumentParser(prog="jda-collect")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create_all 建表")
    sub.add_parser("seed", help="写入首批公司与技能字典")
    sub.add_parser("list", help="列出公司映射表")
    run = sub.add_parser("run", help="执行采集")
    group = run.add_mutually_exclusive_group(required=True)
    group.add_argument("--company", help="按 slug 采集一家")
    group.add_argument("--all", action="store_true", help="采集全部 active 公司")
    args = parser.parse_args()

    if args.cmd == "init":
        init_db()
        print("done: create_all")
        return 0

    with SessionLocal() as session:
        if args.cmd == "seed":
            init_db()
            print("seed:", run_seed(session))
            return 0
        if args.cmd == "list":
            for c in session.execute(select(__import__("app.models", fromlist=["Company"]).Company)).scalars():
                print(f"{c.slug:10s} {c.ats_type:14s} {c.feed_url or c.site_url or ''}")
            return 0
        if args.cmd == "run":
            init_db()
            if args.all:
                results = collect_all(session)
            else:
                from app.models import Company

                company = session.execute(select(Company).where(Company.slug == args.company)).scalars().first()
                if company is None:
                    print(f"error: company '{args.company}' 不存在（先跑 seed）")
                    return 1
                results = [collect_company(session, company)]
            for r in results:
                print(r)
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
