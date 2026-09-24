"""市场洞察报告测试（§12.6 P5 ①）：城市/技能/薪资/趋势聚合 + k-匿名脱敏。"""

from __future__ import annotations

from datetime import date

from app.models import Job
from app.services.market import first_city, market_report, median, percentile, region_of


def _job(source="netease", city="杭州", city_keys=None, skills=None, salary=None, currency="CNY",
         publish=None, external_id="J", title="职位"):
    job = Job(
        external_id=external_id,
        title=title,
        city=city,
        city_keys=city_keys if city_keys is not None else city,
        skills=skills or [],
        apply_url=f"https://x.com/{external_id}",
        source=source,
        status="active",
    )
    if salary is not None:
        job.salary_min, job.salary_max, job.salary_currency = salary[0], salary[1], currency
    if publish is not None:
        job.publish_date = publish
    return job


def test_pure_helpers():
    assert median([1, 3]) == 2.0
    assert median([1, 2, 9]) == 2.0
    assert median([]) is None
    assert percentile([10, 20, 30, 40], 0.25) == 17.5
    assert percentile([10], 0.75) == 10.0
    assert percentile([], 0.5) is None
    # city_keys 优先取首段；回退 city 的首个分隔段
    assert first_city(_job(city_keys="杭州|上海")) == "杭州"
    assert first_city(_job(city="上海、北京", city_keys="")) == "上海"
    assert first_city(_job(city="N/A", city_keys="")) is None


def test_region_of_matches_recommend_scope(session):
    assert region_of(_job(source="netease")) == "cn"
    assert region_of(_job(source="greenhouse")) == "overseas"
    # source IS NULL 的脏数据归海外（与 §4.2 一致，避免 NOT IN 遇 NULL 落空）
    assert region_of(_job(source=None)) == "overseas"


def _seed_market(session):
    """4 条国内（杭州）+ 3 条海外（New York），均带技能与薪资。"""
    for i in range(4):
        session.add(_job(external_id=f"CN{i}", skills=["Python", "SQL"], salary=(20, 40),
                         publish=date(2026, 9, 10)))
    for i in range(3):
        session.add(_job(source="greenhouse", city="New York, NY", city_keys="New York|NYC",
                         external_id=f"US{i}", skills=["Python"], salary=(100, 200),
                         currency="USD", publish=date(2026, 8, 10)))
    session.commit()


def test_report_aggregates_all_dimensions(session):
    _seed_market(session)
    report = market_report(session, now=date(2026, 9, 24), months=3)

    assert report["scope"] == {
        "region": "all", "total_jobs": 7, "with_skills": 7, "with_salary": 7,
        "unknown_city": 0, "as_of": "2026-09-24",
    }
    assert report["cities"]["cn"] == [{"city": "杭州", "count": 4}]
    assert report["cities"]["overseas"] == [{"city": "New York", "count": 3}]
    assert report["skills"][0] == {"skill": "Python", "count": 7, "share": 1.0}
    assert report["skills"][1] == {"skill": "SQL", "count": 4, "share": 0.5714}
    # 薪资按币种分组，只回聚合量（中位数/四分位），不暴露单条
    assert report["salary"]["CNY"]["count"] == 4
    assert report["salary"]["CNY"]["max_median"] == 40.0
    assert report["salary"]["USD"]["max_median"] == 200.0
    # 趋势窗口零值补齐（07 无发布）
    assert report["trend"] == [
        {"month": "2026-07", "count": 0},
        {"month": "2026-08", "count": 3},
        {"month": "2026-09", "count": 4},
    ]


def test_anonymity_suppresses_small_buckets(session):
    _seed_market(session)
    # 单条城市（拉萨）与小样本薪资桶都必须被脱敏掉
    only = _job(external_id="TINY", city="拉萨", city_keys="拉萨", skills=["Go"],
                salary=(50, 60), publish=date(2026, 9, 1))
    session.add(only)
    session.commit()

    report = market_report(session, now=date(2026, 9, 24), top=10)
    assert report["anonymity"]["min_sample"] == 3
    assert report["anonymity"]["suppressed_buckets"] >= 1
    assert all(row["city"] != "拉萨" for row in report["cities"]["cn"])
    # 拉萨的薪资不能通过"拜码头"式分组暴露：CNY 组样本数变 5 仍 ≥3，故此处校验
    # 的是"小样本技能桶被截断"（Go 仅 1 条）与脱敏计数
    assert all(row["skill"] != "Go" for row in report["skills"])


def test_salary_group_below_min_sample_dropped(session):
    session.add(_job(external_id="A", skills=["Python"], salary=(1, 2)))
    session.add(_job(external_id="B", skills=["Python"]))
    session.add(_job(external_id="C", skills=["Python"]))
    session.commit()
    report = market_report(session, now=date(2026, 9, 24))
    assert report["salary"] == {}  # 唯一带薪资的币种组样本 1 < min_sample=3
    assert report["scope"]["with_salary"] == 1


def test_report_region_filter(session):
    _seed_market(session)
    session.add(_job(source=None, city="", city_keys="", external_id="NULLSRC"))
    session.commit()

    cn = market_report(session, region="cn", now=date(2026, 9, 24))
    assert cn["scope"]["total_jobs"] == 4
    assert cn["cities"]["overseas"] == []

    overseas = market_report(session, region="overseas", now=date(2026, 9, 24))
    # 3 条 greenhouse + 1 条 source IS NULL（无城市 → unknown_city）
    assert overseas["scope"]["total_jobs"] == 4
    assert overseas["scope"]["unknown_city"] == 1


def test_market_api(session, client):
    _seed_market(session)
    resp = client.get("/api/insights/market", params={"region": "cn", "months": 3})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["scope"]["total_jobs"] == 4
    assert data["cities"]["cn"][0]["city"] == "杭州"

    assert client.get("/api/insights/market", params={"region": "moon"}).status_code == 422
    assert client.get("/api/insights/market", params={"top": 0}).status_code == 422