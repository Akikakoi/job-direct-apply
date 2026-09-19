"""city 多值升级测试：city_keys 生成单测 + /api/jobs 双列匹配 + 采集侧落列。"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.models import Job
from app.services.city import city_keys, split_city_tokens


# ---------- 单元测试：多值拆分与规范化索引串 ----------


def test_split_city_tokens():
    # 分号/顿号/竖线/斜杠拆分；逗号不拆（"New York, NY" 是州后缀）
    assert split_city_tokens("Menlo Park, CA; New York, NY") == ["Menlo Park, CA", "New York, NY"]
    assert split_city_tokens("上海、北京") == ["上海", "北京"]
    assert split_city_tokens("杭州|苏州") == ["杭州", "苏州"]
    assert split_city_tokens("San Francisco/LA") == ["San Francisco", "LA"]
    assert split_city_tokens("New York, NY") == ["New York, NY"]
    assert split_city_tokens(None) == []
    assert split_city_tokens("  ") == []


def test_city_keys_normalization():
    assert city_keys("杭州市") == "杭州"
    # 别名在存储侧展开：库内存 NYC 也能被 "New York" 召回
    assert city_keys("NYC") == "new york|nyc"
    assert city_keys("上海、北京") == "上海|北京"
    assert city_keys("Menlo Park, CA; New York, NY") == "menlo park, ca|new york, ny"
    assert city_keys(None) is None
    assert city_keys("") is None


def test_city_keys_query_side_hit(session, client):
    """库内存别名 "NYC"，用标准写法 "New York" 查询 → 经 city_keys 召回。"""
    session.add(
        Job(
            external_id="nyc-1",
            title="ny job",
            city="NYC",
            city_keys=city_keys("NYC"),
            apply_url="https://x.com/1",
            source="test",
            status="active",
        )
    )
    session.commit()
    resp = client.get("/api/jobs", params={"city": "New York"})
    assert resp.json()["data"]["total"] == 1


def test_city_keys_fallback_to_raw_city(session, client):
    """未回填 city_keys 的旧行 → 回退原 city 列 contains，行为不变。"""
    session.add(
        Job(
            external_id="legacy-1",
            title="hz job",
            city="杭州市",
            city_keys=None,
            apply_url="https://x.com/2",
            source="test",
            status="active",
        )
    )
    session.commit()
    assert client.get("/api/jobs", params={"city": "杭州"}).json()["data"]["total"] == 1
    assert client.get("/api/jobs", params={"city": "乌鲁木齐"}).json()["data"]["total"] == 0


def test_collect_writes_city_keys(session):
    """采集侧 upsert 时同步生成 city_keys。"""
    from app.adapters.base import NormalizedJob
    from app.models import Company
    from app.services.collect import collect_company

    class FakeAdapter:
        ats_type = "fake"

        def __init__(self, jobs):
            self.jobs = jobs

        def normalize_all(self, company):
            return self.jobs

    company = Company(slug="acme", name="Acme", ats_type="fake", fetch_policy={"interval_min": 60})
    session.add(company)
    session.commit()

    jobs = [
        NormalizedJob(
            external_id="J1", title="多城市", apply_url="https://x.com/1", source="fake",
            city="杭州市；上海市",
        ),
        NormalizedJob(
            external_id="J2", title="别名", apply_url="https://x.com/2", source="fake", city="NYC",
        ),
    ]
    mp = pytest.MonkeyPatch()
    mp.setattr("app.services.collect.get_adapter", lambda t, client=None: FakeAdapter(jobs))
    try:
        result = collect_company(session, company, now=datetime(2026, 9, 19, 12, 0, 0))
    finally:
        mp.undo()
    assert result["status"] == "ok" and result["inserted"] == 2

    rows = {j.external_id: j for j in session.query(Job).all()}
    assert rows["J1"].city_keys == "上海|杭州"
    assert rows["J2"].city_keys == "new york|nyc"
