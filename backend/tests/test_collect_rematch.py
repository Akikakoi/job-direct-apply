"""职位变更后 match_score 失效重算测试（挂账销项）。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import Company, Job, MatchScore, Resume
from app.services.collect import collect_company


class FakeAdapter:
    ats_type = "fake"

    def __init__(self, jobs):
        self.jobs = jobs

    def normalize_all(self, company):
        return self.jobs


def make_company(session, slug="acme") -> Company:
    company = Company(slug=slug, name="Acme", ats_type="fake", fetch_policy={"interval_min": 60})
    session.add(company)
    session.commit()
    return company


def make_norm(external_id, title):
    from app.adapters.base import NormalizedJob

    return NormalizedJob(external_id=external_id, title=title, apply_url="https://x.com/1", source="fake")


def _patch_adapter(monkeypatch, jobs):
    adapter = FakeAdapter(jobs)
    monkeypatch.setattr("app.services.collect.get_adapter", lambda t, client=None: adapter)


def test_collect_triggers_rematch_on_insert(session, monkeypatch):
    """新职位入库 → 该简历全量重算 match_scores。"""
    resume = Resume(user_id=1, profile={"skills": ["python"], "target_role": "后端工程师"})
    session.add(resume)
    session.commit()
    company = make_company(session)
    _patch_adapter(monkeypatch, [make_norm("J1", "Python 后端工程师")])

    result = collect_company(session, company, now=datetime(2026, 9, 19, 12, 0, 0))
    assert result["status"] == "ok"
    assert result["inserted"] == 1
    assert result["rematched"] == 1  # 1 份简历 × 1 个 active 职位

    rows = session.execute(select(MatchScore).where(MatchScore.resume_id == resume.id)).scalars().all()
    assert len(rows) == 1
    assert rows[0].job_id == session.query(Job).one().id


def test_collect_rematch_skipped_when_no_change(session, monkeypatch):
    """职位无增无改且无简历 → 不触发重算（结果无 rematched 键）。"""
    company = make_company(session)
    _patch_adapter(monkeypatch, [])
    result = collect_company(session, company, now=datetime(2026, 9, 19, 12, 0, 0))
    assert result["status"] == "ok"
    assert "rematched" not in result


def test_collect_rematch_off_switch(session, monkeypatch):
    """rematch=False 时即使有变更也不重算（采集大批量时手动控制）。"""
    resume = Resume(user_id=1, profile={"skills": ["python"]})
    session.add(resume)
    session.commit()
    company = make_company(session)
    _patch_adapter(monkeypatch, [make_norm("J1", "A")])

    result = collect_company(session, company, now=datetime(2026, 9, 19, 12, 0, 0), rematch=False)
    assert "rematched" not in result
    assert session.query(MatchScore).count() == 0


def test_refresh_matches_all_shared_index(session):
    """多份简历共享一次 TF-IDF 索引（对比逐份 refresh 的等价性）。"""
    from app.pipelines.match import refresh_matches, refresh_matches_all

    jobs = [
        Job(external_id=f"J{i}", title=f"Python 后端工程师{i}", apply_url="https://x.com/1",
            source="test", status="active", skills=["python", "fastapi"])
        for i in range(3)
    ]
    session.add_all(jobs)
    resumes = [
        Resume(user_id=1, profile={"skills": ["python"], "target_role": "后端工程师"}),
        Resume(user_id=2, profile={"skills": ["java"], "target_role": "前端工程师"}),
    ]
    session.add_all(resumes)
    session.commit()

    total = refresh_matches_all(session)
    assert total == 6  # 2 份简历 × 3 个职位

    # 逐份 refresh 结果与批量等价（同 profile 同分）
    for resume in resumes:
        before = {
            ms.job_id: (float(ms.rule_score), float(ms.final_score))
            for ms in session.execute(select(MatchScore).where(MatchScore.resume_id == resume.id)).scalars()
        }
        refresh_matches(session, resume)
        after = {
            ms.job_id: (float(ms.rule_score), float(ms.final_score))
            for ms in session.execute(select(MatchScore).where(MatchScore.resume_id == resume.id)).scalars()
        }
        assert before == after
