"""采集编排测试：upsert 去重幂等、间隔限流、TTL 失效。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import Company, FetchLog, Job
from app.services.collect import canonicalize_skills, collect_company


class FakeAdapter:
    """可注入的假适配器：返回固定 NormalizedJob 列表。"""

    ats_type = "fake"

    def __init__(self, jobs):
        self.jobs = jobs
        self.calls = 0

    def discover(self, company):
        self.calls += 1
        return self.jobs

    def normalize_raw(self, raw):
        return raw

    def normalize_all(self, company):
        self.discover(company)
        return self.jobs


def make_company(session, slug="acme", ats_type="fake") -> Company:
    company = Company(slug=slug, name=slug.title(), ats_type=ats_type, fetch_policy={"interval_min": 60})
    session.add(company)
    session.commit()
    return company


def make_norm(external_id, title):
    from app.adapters.base import NormalizedJob

    return NormalizedJob(external_id=external_id, title=title, apply_url="https://x.com/1", source="fake")


def test_upsert_dedup_and_idempotent(session):
    """§5.2/§9：同一职位重复采集不重复入库；二次运行为 update。"""
    from app.adapters.base import RawJob

    jobs = [make_norm("J1", "A"), make_norm("J2", "B"), make_norm("J1", "A-dup")]
    adapter = FakeAdapter(jobs)
    company = make_company(session)

    monkey_adapter = pytest.MonkeyPatch()
    monkey_adapter.setattr("app.services.collect.get_adapter", lambda t, client=None: adapter)
    try:
        now1 = datetime(2026, 9, 16, 12, 0, 0)
        r1 = collect_company(session, company, now=now1)
        assert r1["status"] == "ok" and r1["inserted"] == 2 and r1["discovered"] == 3

        now2 = now1 + timedelta(hours=2)  # 超过 interval，允许再次采集
        r2 = collect_company(session, company, now=now2)
        assert r2["status"] == "ok" and r2["inserted"] == 0 and r2["updated"] == 2
    finally:
        monkey_adapter.undo()

    rows = session.execute(select(Job)).scalars().all()
    assert len(rows) == 2  # 去重
    assert {j.external_id for j in rows} == {"J1", "J2"}
    assert rows[0].updated_at >= now2 - timedelta(seconds=1)  # 保鲜时间戳已刷新


def test_interval_guard_skips(session):
    from app.adapters.base import RawJob

    adapter = FakeAdapter([make_norm("J1", "A")])
    company = make_company(session)
    mp = pytest.MonkeyPatch()
    mp.setattr("app.services.collect.get_adapter", lambda t, client=None: adapter)
    try:
        now = datetime(2026, 9, 16, 12, 0, 0)
        assert collect_company(session, company, now=now)["status"] == "ok"
        r = collect_company(session, company, now=now + timedelta(minutes=10))
        assert r["status"] == "skipped"
        assert adapter.calls == 1  # 未真实抓取
    finally:
        mp.undo()


def test_ttl_expires_stale_jobs(session):
    adapter = FakeAdapter([])  # 本轮什么都抓不到
    company = make_company(session)

    old_job = Job(company_id=company.id, external_id="OLD", title="Old", apply_url="https://x.com/o",
                  source="fake", status="active")
    session.add(old_job)
    session.commit()
    # 手动把 updated_at 拨回 5 小时前（interval=60, multiplier=3 → TTL=180min）
    session.execute(
        Job.__table__.update().where(Job.id == old_job.id).values(updated_at=datetime.utcnow() - timedelta(hours=5))
    )
    session.commit()

    mp = pytest.MonkeyPatch()
    mp.setattr("app.services.collect.get_adapter", lambda t, client=None: adapter)
    try:
        r = collect_company(session, company, now=datetime.utcnow())
        assert r["expired"] == 1
    finally:
        mp.undo()

    refreshed = session.get(Job, old_job.id)
    assert refreshed.status == "expired"


def test_fetch_log_written(session):
    adapter = FakeAdapter([make_norm("J1", "A")])
    company = make_company(session)
    mp = pytest.MonkeyPatch()
    mp.setattr("app.services.collect.get_adapter", lambda t, client=None: adapter)
    try:
        collect_company(session, company, now=datetime.utcnow())
    finally:
        mp.undo()

    log = session.execute(select(FetchLog)).scalars().one()
    assert log.status == "success" and log.job_count == 1 and log.finished_at is not None


def test_canonicalize_skills():
    alias_map = {"k8s": "kubernetes", "kubernetes": "kubernetes", "go": "go"}
    out = canonicalize_skills(["K8s", "k8s", "Go", "Rust"], alias_map)
    assert out == ["go", "kubernetes", "rust"]  # 归一 + 去重 + 未命中保留
