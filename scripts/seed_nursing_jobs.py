# -*- coding: utf-8 -*-
"""种一批国内医疗护理测试岗位（source=demo），让护理/医学简历"有岗可推"。

背景（第二十九轮实测）：岗位池 4791 条全来自互联网/科技公司（网易 2629 +
海外 ATS），护理简历 role 恒 0、skill 零交集，全池滑向 24~25 分底噪，推荐
退化为随机。本脚本补演示数据；配套改动：match.py 医疗职能族 + main.py
DOMESTIC_SOURCES 登记 "demo"。

用法（backend/ 下）：
    ../.venv/Scripts/python.exe scripts/seed_nursing_jobs.py

幂等：公司按 slug upsert，岗位按 (company_id, external_id) upsert；
status 恒置 active。重跑只更新，不产生重复行。
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

# 保证 backend/ 在 sys.path（脚本也可从工作区根目录跑）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from sqlalchemy import select  # noqa: E402

from app.core.db import SessionLocal  # noqa: E402
from app.models import Company, Job  # noqa: E402

SLUG = "demo-hospitals"
NAME = "演示医疗集团（测试数据）"

# 技能标签对齐简历解析侧的归一标签（简历24 实测：无菌技术/静脉穿刺/心肺复苏/
# 护理病历书写/交接班记录书写/医院护理信息系统等），保证 skill 项能拉开分差。
JOBS = [
    dict(external_id="demo-nur-001", title="临床护士（内科）", city="杭州",
         skills=["无菌技术", "静脉穿刺", "护理病历书写", "交接班记录书写", "医院护理信息系统"],
         experience_min=None, degree_req="bachelor", smin=6000, smax=9000),
    dict(external_id="demo-nur-002", title="临床护士（手术室）", city="南京",
         skills=["无菌技术", "心肺复苏", "器械清点", "交接班记录书写", "沟通协作"],
         experience_min=1, degree_req="bachelor", smin=7000, smax=11000),
    dict(external_id="demo-nur-003", title="ICU 护士", city="上海",
         skills=["心肺复苏", "重症监护", "静脉穿刺", "呼吸机使用", "护理病历书写"],
         experience_min=2, degree_req="bachelor", smin=9000, smax=14000),
    dict(external_id="demo-nur-004", title="急诊科护士", city="成都",
         skills=["心肺复苏", "急救包扎", "静脉穿刺", "沟通协作", "交接班记录书写"],
         experience_min=1, degree_req="bachelor", smin=7000, smax=10000),
    dict(external_id="demo-nur-005", title="护理部主任", city="北京",
         skills=["护理管理", "护理病历书写", "护理质控", "沟通协作", "医院护理信息系统"],
         experience_min=5, degree_req="bachelor", smin=15000, smax=22000),
    dict(external_id="demo-nur-006", title="手术室器械护士", city="广州",
         skills=["无菌技术", "器械清点", "心肺复苏", "交接班记录书写"],
         experience_min=None, degree_req="na", smin=6500, smax=9500),
    dict(external_id="demo-nur-007", title="社区诊所护士", city="苏州",
         skills=["静脉穿刺", "无菌技术", "健康宣教", "沟通协作", "office 办公软件"],
         experience_min=None, degree_req="na", smin=5000, smax=7500),
    dict(external_id="demo-nur-008", title="药剂师", city="武汉",
         skills=["处方审核", "药品调剂", "药事管理", "医院护理信息系统"],
         experience_min=1, degree_req="bachelor", smin=7000, smax=11000),
]


def main() -> None:
    with SessionLocal() as session:
        company = session.execute(select(Company).where(Company.slug == SLUG)).scalar_one_or_none()
        if company is None:
            company = Company(
                slug=SLUG, name=NAME, ats_type="demo", site_url=None, feed_url=None,
                locale="zh-CN", auth_type="none", is_active=True,
            )
            session.add(company)
            session.flush()
            print(f"[seed] 新建公司 {NAME} (id={company.id})")
        else:
            print(f"[seed] 公司已存在 (id={company.id})")

        today = date.today()
        for spec in JOBS:
            job = session.execute(
                select(Job).where(Job.company_id == company.id, Job.external_id == spec["external_id"])
            ).scalar_one_or_none()
            fields = dict(
                title=spec["title"], city=spec["city"], city_keys=spec["city"],
                skills=spec["skills"], experience_min=spec["experience_min"],
                degree_req=spec["degree_req"], salary_min=spec["smin"], salary_max=spec["smax"],
                salary_currency="CNY",
                description=f"【测试种子岗位】{NAME} {spec['title']}（{spec['city']}），用于验证医疗护理简历的匹配推荐链路。",
                apply_url="https://example.com/demo-apply", can_auto_apply=False,
                source="demo", status="active", publish_date=today,
            )
            if job is None:
                job = Job(company_id=company.id, external_id=spec["external_id"], **fields)
                session.add(job)
                action = "新增"
            else:
                for k, v in fields.items():
                    setattr(job, k, v)
                action = "更新"
            print(f"[seed] {action} {spec['title']}（{spec['city']}）")
        session.commit()
    print("[seed] 完成")


if __name__ == "__main__":
    main()
