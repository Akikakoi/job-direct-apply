"""Ashby / SmartRecruiters 适配器测试（P4）：MockTransport 不触网。"""

from __future__ import annotations

import httpx
import pytest

from app.adapters.ashby import AshbyAdapter, parse_ashby_org
from app.adapters.smartrecruiters import SmartRecruitersAdapter, parse_sr_company
from app.models import Company

ASHBY_JOB = {
    "id": "abc-123",
    "title": "Senior Backend Engineer",
    "location": "Remote - Global",
    "isListed": True,
    "isRemote": True,
    "jobUrl": "https://jobs.ashbyhq.com/ashby/abc-123",
    "applyUrl": "https://jobs.ashbyhq.com/ashby/abc-123/application",
    "descriptionPlain": "We are hiring a backend engineer. Python required.",
    "publishedAt": "2026-09-01T00:00:00.000Z",
}
SR_POSTING = {
    "id": "744000150235108",
    "name": "Backend Developer, Platform",
    "location": {"city": "Vancouver", "country": "Canada", "remote": False},
    "releasedDate": "2026-09-18T01:06:47.685Z",
}


def _company(slug: str, ats: str, feed_url: str | None = None) -> Company:
    return Company(slug=slug, name=slug, ats_type=ats, feed_url=feed_url, is_active=True)


def test_parse_slugs():
    assert parse_ashby_org("https://jobs.ashbyhq.com/elevenlabs", "x") == "elevenlabs"
    assert parse_ashby_org(None, "ashby") == "ashby"
    assert parse_sr_company("https://jobs.smartrecruiters.com/Equinox", "x") == "Equinox"
    assert parse_sr_company(None, "equinox") == "equinox"


def test_ashby_discover_and_normalize():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "posting-api/job-board/ashby" in str(request.url)
        return httpx.Response(200, json={"apiVersion": "1", "jobs": [ASHBY_JOB, {"id": "x2", "isListed": False}]})

    adapter = AshbyAdapter(client=httpx.Client(transport=httpx.MockTransport(handler)))
    company = _company("ashby", "ashby", "https://jobs.ashbyhq.com/ashby")
    raws = adapter.discover(company)
    assert len(raws) == 1  # isListed=False 被过滤

    n = adapter.normalize_raw(raws[0])
    assert n.external_id == "abc-123"
    assert n.title == "Senior Backend Engineer"
    assert n.city == "Remote - Global"
    assert "Python required." in (n.description or "")
    assert n.apply_url == "https://jobs.ashbyhq.com/ashby/abc-123"
    assert str(n.publish_date) == "2026-09-01"
    assert n.to_row()["source"] == "ashby"


def test_sr_pagination_and_normalize():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        offset = int(request.url.params.get("offset", 0))
        if offset == 0:
            content = [SR_POSTING] * 100
            return httpx.Response(200, json={"offset": 0, "limit": 100, "totalFound": 101, "content": content})
        return httpx.Response(200, json={"offset": 100, "limit": 100, "totalFound": 101, "content": [SR_POSTING]})

    adapter = SmartRecruitersAdapter(client=httpx.Client(transport=httpx.MockTransport(handler)))
    company = _company("equinox", "smartrecruiters", "https://jobs.smartrecruiters.com/Equinox")
    raws = adapter.discover(company)
    assert calls["n"] == 2  # 两页翻完
    assert len(raws) == 101

    n = adapter.normalize_raw(raws[0])
    assert n.external_id == "744000150235108"
    assert n.title == "Backend Developer, Platform"
    assert n.city == "Vancouver, Canada"
    assert n.apply_url == "https://jobs.smartrecruiters.com/Equinox/744000150235108"
    assert n.description is None  # 详情 N+1 一期不取


def test_sr_remote_location():
    adapter = SmartRecruitersAdapter(client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))
    n = adapter.normalize_raw(
        type("R", (), {"payload": {**SR_POSTING, "location": {"remote": True}}, "feed_url": None, "company_slug": "equinox"})()
    )
    assert n.city == "Remote"


def test_registry_has_new_types():
    from app.adapters.registry import get_adapter

    assert get_adapter("ashby").ats_type == "ashby"
    assert get_adapter("smartrecruiters").ats_type == "smartrecruiters"
    with pytest.raises(KeyError):
        get_adapter("moka")
