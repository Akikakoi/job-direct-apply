"""离线批量补职位 skills（P2 数据质量项）。

背景：netease 适配器只存了 description/requirement 全文，skills 标签为空，
导致匹配时 skill_hit 全部走中性分 0.5。本脚本用 skill_tags 字典 + 内置别名
对 description 做词典扫描（与简历侧同一套归一口径），零 LLM 成本。

用法（backend/ 下）：
    ../.venv/Scripts/python.exe ../scripts/extract_job_skills.py [--dry-run]

只处理 skills 为空且 description 非空的 active 职位；dry-run 只统计不改库。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from sqlalchemy import select  # noqa: E402

from app.core.db import SessionLocal, init_db  # noqa: E402
from app.models import Job  # noqa: E402
from app.pipelines.rules import scan_skills  # noqa: E402
from app.services.collect import build_alias_map, canonicalize_skills  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只统计不写库")
    args = parser.parse_args()

    init_db()
    with SessionLocal() as session:
        alias_map = build_alias_map(session)
        jobs = (
            session.execute(
                select(Job).where(
                    Job.status == "active",
                    Job.description.isnot(None),
                    (Job.skills.is_(None)) | (Job.skills == []),
                )
            )
            .scalars()
            .all()
        )
        print(f"待处理职位: {len(jobs)}")

        updated = 0
        tag_count: dict[str, int] = {}
        for job in jobs:
            text = f"{job.title}\n{job.description or ''}"
            hits = canonicalize_skills(scan_skills(text, alias_map), alias_map)
            if not hits:
                continue
            if not args.dry_run:
                job.skills = hits
            updated += 1
            for t in hits:
                tag_count[t] = tag_count.get(t, 0) + 1

        if args.dry_run:
            session.rollback()
        else:
            session.commit()

        print(f"补上标签的职位: {updated} / {len(jobs)}")
        print("标签分布 top15:")
        for tag, n in sorted(tag_count.items(), key=lambda x: -x[1])[:15]:
            print(f"  {tag}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
