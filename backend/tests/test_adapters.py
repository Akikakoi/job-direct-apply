"""适配器测试：字段映射幂等、Workday 分页与 limit=20 陷阱、robots 解析、网易 JSON 分页。"""

from __future__ import annotations

import httpx
import pytest

import tests.fakes as fakes
from app.adapters.greenhouse import GreenhouseAdapter, parse_greenhouse_token
from app.adapters.lever import LeverAdapter, parse_lever_slug
from app.adapters.netease import NeteaseAdapter, parse_netease_base, parse_work_years
from app.adapters.official_site import parse_robots
from app.adapters.workday import WorkdayAdapter, parse_workday_site


def test_parse_workday_site():
    tenant, shard, site = parse_workday_site(
        "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"
    )
    assert (tenant, shard, site) == ("nvidia", "wd5", "NVIDIAExternalCareerSite")


def test_parse_workday_site_with_locale_path():
    _, _, site = parse_workday_site(
        "https://salesforce.wd12.myworkdayjobs.com/en-US/External_Career_Site"
    )
    assert site == "External_Career_Site"


def test_parse_workday_site_invalid():
    with pytest.raises(ValueError):
        parse_workday_site("https://example.com/careers")


def test_workday_pagination_sends_limit_20():
    """坑①：请求体 limit 必须=20；坑②：按 total 停止翻页。"""
    seen_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_bodies.append(request.read())
        # 用调用序号区分页（POST 无 query 参数）
        call = len(seen_bodies)
        if call == 1:
            return httpx.Response(200, json=fakes.WORKDAY_PAGE_1)
        return httpx.Response(200, json=fakes.WORKDAY_PAGE_2)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = WorkdayAdapter(client=client)
    company = type("C", (), {"slug": "nvidia", "feed_url": "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"})()
    raws = adapter.discover(company)

    assert len(raws) == 21  # 20 + 1
    first_body = seen_bodies[0].decode()
    assert '"limit": 20' in first_body or '"limit":20' in first_body


def test_workday_normalize():
    adapter = WorkdayAdapter()
    n = adapter.normalize_raw(fakes.workday_raws()[0])
    assert n.external_id == "JR2021061"
    assert n.apply_url == "https://nvidia.wd5.myworkdayjobs.com/job/Vietnam-Hanoi/Director--Engineering---Software-Engineering_JR2021061"
    assert n.city == "Vietnam, Hanoi"
    assert "k8s" in n.skills and "cuda" in n.skills
    # 幂等：两次 normalize 结果一致
    assert n == adapter.normalize_raw(fakes.workday_raws()[0])


def test_greenhouse_token_and_normalize():
    token = parse_greenhouse_token("https://boards-api.greenhouse.io/v1/boards/stripe/jobs")
    assert token == "stripe"

    adapter = GreenhouseAdapter()
    n = adapter.normalize_raw(fakes.greenhouse_raws()[0])
    assert n.external_id == "6606581"
    assert n.city == "Tokyo, Japan"
    assert str(n.publish_date) == "2026-09-10"
    assert n == adapter.normalize_raw(fakes.greenhouse_raws()[0])  # 幂等


def test_lever_slug_and_normalize():
    assert parse_lever_slug("https://api.lever.co/v0/postings/twitch?mode=json", "x") == "twitch"
    assert parse_lever_slug(None, "twitch") == "twitch"

    adapter = LeverAdapter()
    n = adapter.normalize_raw(fakes.lever_raws()[0])
    assert n.external_id == "a1b2c3d4"
    assert n.title == "Software Engineer, Media"
    assert str(n.publish_date) == "2026-09-14"
    assert n.description == "Build live video systems."


def test_robots_parse():
    robots = """
User-agent: *
Disallow: /private/
Allow: /jobs/

User-agent: badbot
Disallow: /
"""
    assert parse_robots(robots, "https://x.com/jobs/1", ua="job-direct-apply-bot/0.1") is True
    assert parse_robots(robots, "https://x.com/private/a", ua="job-direct-apply-bot/0.1") is False
    assert parse_robots(robots, "https://x.com/jobs/1", ua="badbot") is False


def test_parse_work_years():
    assert parse_work_years("3-5年") == 3
    assert parse_work_years("5年以上") == 5
    assert parse_work_years("1年以下") == 0
    assert parse_work_years("不限") is None
    assert parse_work_years("应届毕业生") is None
    assert parse_work_years(None) is None


def test_netease_base():
    assert parse_netease_base("https://hr.163.com/", "https://x.com") == "https://hr.163.com"
    assert parse_netease_base(None, "https://hr.163.com") == "https://hr.163.com"
    assert parse_netease_base(None, None) == "https://hr.163.com"


def test_netease_pagination_and_request_shape():
    """分页按 data.pages 停止；请求为 POST JSON 且 pageSize=50。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = __import__("json").loads(request.content)
        if body.get("currentPage") == 1:
            return httpx.Response(200, json=fakes.NETEASE_PAGE_1)
        return httpx.Response(200, json=fakes.NETEASE_PAGE_2)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = NeteaseAdapter(client=client)
    company = type("C", (), {"slug": "netease", "site_url": "https://hr.163.com",
                             "feed_url": "https://hr.163.com"})()
    raws = adapter.discover(company)

    assert len(raws) == 3  # 2 + 1
    assert len(seen) == 2  # 按 pages=2 翻两页即停
    assert seen[0].method == "POST"
    assert str(seen[0].url) == "https://hr.163.com/api/hr163/position/queryPage"
    first_body = seen[0].read().decode()
    assert '"pageSize": 50' in first_body or '"pageSize":50' in first_body


def test_netease_normalize():
    adapter = NeteaseAdapter()
    raws = fakes.netease_raws()

    n1 = adapter.normalize_raw(raws[0])
    assert n1.external_id == "58384"
    assert n1.city == "杭州市"
    assert n1.apply_url == "https://hr.163.com/job-detail.html?id=58384&lang=zh"  # beeUrl=None 兜底
    assert n1.degree_req == "本科"
    assert n1.experience_min is None  # 不限
    assert "负责平台开发" in n1.description and "熟悉 Python" in n1.description
    assert str(n1.publish_date) == "2026-09-15"
    assert n1.source == "netease"
    assert n1 == adapter.normalize_raw(raws[0])  # 幂等

    n2 = adapter.normalize_raw(raws[1])
    assert n2.city == "杭州市、广州"  # 多工作地顿号连接
    assert n2.apply_url == "https://hr.163.com/job-detail.html?id=69515"  # beeUrl 优先
    assert n2.degree_req is None  # 不限 → None
    assert n2.experience_min == 3  # 3-5年 → 3
    assert n2.description == "搭建世界观。"  # 无 requirement 时只保留描述

    n3 = adapter.normalize_raw(raws[2])
    assert n3.experience_min == 0  # 1年以下
    assert n3.publish_date is None  # updateTime 缺失容忍
    assert n3.description is None
