"""官网兜底适配器用例（§5.3 规则化解析）：JSON-LD / 内嵌 JSON / 逐字段正则。

重点覆盖两件容易"看着能跑、实际会出事"的事：
1. **宁可少字段不可错字段**——规则命不中就留空，不拿占位串当数据（错字段会污染匹配）；
2. **合规前置**——robots 不允许就抛错停手（不重试、不绕过），且不能把 404 robots 当成"禁止"。
"""

from __future__ import annotations

import httpx
import pytest

from app.adapters.base import RawJob
from app.adapters.official_site import (
    OfficialSiteAdapter,
    OfficialSiteNotConfigured,
    RobotsDisallowed,
    find_job_postings,
    get_path,
    html_to_text,
    json_ld_blocks,
    parse_robots,
    regex_fields,
    robots_allowed,
)
from app.adapters.registry import get_adapter
from app.models import Company

ROBOTS_ALLOW = "User-agent: *\nAllow: /"
ROBOTS_DENY = "User-agent: *\nDisallow: /careers"

JSON_LD_PAGE = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"JobPosting",
 "title":"Senior Backend Engineer",
 "url":"https://acme.example.com/jobs/senior-backend?gh_jid=9001",
 "datePosted":"2026-09-10",
 "description":"&lt;div&gt;Build &lt;b&gt;APIs&lt;/b&gt; with Python&lt;/div&gt;",
 "jobLocation":{"address":{"addressLocality":"Hangzhou","addressCountry":"CN"}},
 "baseSalary":{"currency":"CNY","value":{"minValue":300000,"maxValue":480000}}}
</script>
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
  {"@type":"Organization","name":"Acme"},
  {"@type":"JobPosting","title":"Data Engineer","url":"https://acme.example.com/jobs/data",
   "jobLocation":{"address":{"addressLocality":"Shanghai"}}}
]}
</script>
</head><body>jobs</body></html>
"""


def make_company(rules: dict | None = None, **kwargs) -> Company:
    policy = {"interval_min": 720}
    if rules is not None:
        policy["html_rules"] = rules
    return Company(
        slug=kwargs.pop("slug", "acme"),
        name="Acme",
        ats_type="official_site",
        site_url=kwargs.pop("site_url", "https://acme.example.com/careers"),
        feed_url=kwargs.pop("feed_url", None),
        fetch_policy=kwargs.pop("fetch_policy", policy),
    )


def make_adapter(pages: dict[str, str], robots: str | None = ROBOTS_ALLOW) -> OfficialSiteAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            if robots is None:
                return httpx.Response(404, text="not found")
            return httpx.Response(200, text=robots)
        body = pages.get(url)
        if body is None:
            return httpx.Response(404, text="missing page")
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    return OfficialSiteAdapter(client=httpx.Client(transport=httpx.MockTransport(handler)))


# ---------- 纯函数 ----------


def test_parse_robots_allow_and_deny():
    assert parse_robots(ROBOTS_ALLOW, "https://acme.example.com/careers") is True
    assert parse_robots(ROBOTS_DENY, "https://acme.example.com/careers/jobs") is False


def test_robots_404_and_network_error_are_not_treated_as_deny():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert robots_allowed(client, "https://acme.example.com", "https://acme.example.com/careers", "*") is True
    assert robots_allowed(make_adapter({}, robots=None)._client, "https://acme.example.com", "https://x/y", "*") is True


def test_html_to_text_strips_scripts_and_decodes_entities():
    raw = '<div>Hello <b>World</b> &amp; friends</div><script>var x = "jobs";</script><style>.a{}</style>'
    text = html_to_text(raw)
    assert text == "Hello World & friends"
    assert "var x" not in text and ".a{}" not in text


def test_get_path_supports_nested_and_list_index():
    assert get_path({"a": {"b": [{"c": 1}]}}, "a.b.0.c") == 1
    assert get_path({"a": 1}, "a.missing") is None
    assert get_path({"a": [1]}, "a.9") is None
    assert get_path({"a": 1}, None) == {"a": 1}


def test_find_job_postings_walks_graph_and_arrays():
    blocks = json_ld_blocks(JSON_LD_PAGE)
    assert len(blocks) == 2  # 普通块 + @graph 块
    postings = find_job_postings(blocks)
    assert [p["title"] for p in postings] == ["Senior Backend Engineer", "Data Engineer"]
    assert find_job_postings([{"@type": "Organization"}]) == []


def test_json_ld_blocks_ignores_malformed_json():
    html = '<script type="application/ld+json">{not json}</script>'
    assert json_ld_blocks(html) == []


def test_regex_fields_skips_missing_and_bad_patterns():
    html = "<h1>Backend Engineer</h1><span>Hangzhou</span>"
    found = regex_fields(
        html,
        {
            "title": {"regex": r"<h1>(.*?)</h1>", "html": False},
            "absent": {"regex": r"<h2>(.*?)</h2>"},
            "broken": {"regex": "("},
        },
    )
    assert found == {"title": "Backend Engineer"}


# ---------- JSON-LD 主路径 ----------


def test_discover_and_normalize_json_ld():
    url = "https://acme.example.com/careers"
    adapter = make_adapter({url: JSON_LD_PAGE})
    raws = adapter.discover(make_company({"list_url": url}))
    assert len(raws) == 2

    job = adapter.normalize_raw(raws[0])
    assert job.external_id == "9001"  # 从链接查询串取 id
    assert job.title == "Senior Backend Engineer"
    assert job.city == "Hangzhou, CN"
    assert job.publish_date is not None and str(job.publish_date) == "2026-09-10"
    assert job.salary_min == 300000 and job.salary_max == 480000 and job.salary_currency == "CNY"
    assert job.description == "Build APIs with Python"  # 标签剥净、实体还原
    assert job.apply_url == "https://acme.example.com/jobs/senior-backend?gh_jid=9001"
    assert job.source == "official_site" and job.skills == [] and job.can_auto_apply is False


def test_relative_url_is_resolved_against_page():
    page = """
    <script type="application/ld+json">
    {"@type":"JobPosting","title":"PM","url":"/jobs/pm-1"}
    </script>
    """
    url = "https://acme.example.com/careers"
    adapter = make_adapter({url: page})
    job = adapter.normalize_raw(adapter.discover(make_company({"list_url": url}))[0])
    assert job.apply_url == "https://acme.example.com/jobs/pm-1"
    assert job.external_id == "pm-1"  # 无查询 id 时取末段路径
    assert job.city is None and job.publish_date is None  # 缺就留空，不猜


def test_pagination_with_page_placeholder():
    rules = {"list_url": "https://acme.example.com/jobs?page={page}", "max_pages": 5}
    one = '<script type="application/ld+json">{"@type":"JobPosting","title":"A","url":"/j/a"}</script>'
    two = '<script type="application/ld+json">{"@type":"JobPosting","title":"B","url":"/j/b"}</script>'
    adapter = make_adapter(
        {
            "https://acme.example.com/jobs?page=1": one,
            "https://acme.example.com/jobs?page=2": two,
            "https://acme.example.com/jobs?page=3": "<html>none</html>",
        }
    )
    raws = adapter.discover(make_company(rules))
    assert [r.payload["title"] for r in raws] == ["A", "B"]  # 第 3 页空 → 停止


def test_max_pages_is_respected():
    rules = {"list_url": "https://acme.example.com/jobs?page={page}", "max_pages": 1}
    one = '<script type="application/ld+json">{"@type":"JobPosting","title":"A","url":"/j/a"}</script>'
    adapter = make_adapter({"https://acme.example.com/jobs?page=1": one})
    assert len(adapter.discover(make_company(rules))) == 1


def test_next_page_following():
    page1 = """
    <script type="application/ld+json">{"@type":"JobPosting","title":"A","url":"/j/a"}</script>
    <a class="next" href="/careers?p=2">Next</a>
    """
    page2 = '<script type="application/ld+json">{"@type":"JobPosting","title":"B","url":"/j/b"}</script>'
    rules = {
        "list_url": "https://acme.example.com/careers",
        "next_page": {"regex": '<a class="next"[^>]*href="([^"]+)"'},
    }
    adapter = make_adapter({"https://acme.example.com/careers": page1, "https://acme.example.com/careers?p=2": page2})
    raws = adapter.discover(make_company(rules))
    assert [r.payload["title"] for r in raws] == ["A", "B"]


# ---------- 内嵌 JSON（SPA）与正则 ----------


def test_embedded_json_mode_maps_fields_by_path():
    page = """
    <script id="__NEXT_DATA__" type="application/json">
    {"props":{"pageProps":{"jobs":[
      {"name":"Backend Engineer","location":{"city":"Beijing"},"applyUrl":"https://acme.example.com/jobs/1","summary":"<p>Build</p>"},
      {"name":"SRE","location":{"city":"Remote"},"applyUrl":"https://acme.example.com/jobs/2","summary":""}
    ]}}}
    </script>
    """
    rules = {
        "list_url": "https://acme.example.com/careers",
        "json": {"script_id": "__NEXT_DATA__", "json_path": "props.pageProps.jobs"},
        "fields": {
            "title": "name",
            "city": "location.city",
            "url": "applyUrl",
            "description": "summary",
        },
    }
    adapter = make_adapter({"https://acme.example.com/careers": page})
    raws = adapter.discover(make_company(rules))
    assert [r.payload["title"] for r in raws] == ["Backend Engineer", "SRE"]
    job = adapter.normalize_raw(raws[1])
    assert job.title == "SRE" and job.city == "Remote"
    assert job.description is None  # 空字符串不当作描述
    assert job.external_id == "2"


def test_regex_mode_extracts_single_job_page():
    page = """
    <html><body>
    <h1 class="job-title">Staff Platform Engineer</h1>
    <div class="meta"><span class="city">Shenzhen</span></div>
    <div class="content"><p>Kubernetes, Go</p></div>
    </body></html>
    """
    rules = {
        "list_url": "https://acme.example.com/jobs/42",
        "fields": {
            "title": {"regex": '<h1 class="job-title">(.*?)</h1>', "html": False},
            "city": {"regex": '<span class="city">(.*?)</span>', "html": False},
            "description": {"regex": '<div class="content">(.*?)</div>'},
        },
    }
    adapter = make_adapter({"https://acme.example.com/jobs/42": page})
    job = adapter.normalize_raw(adapter.discover(make_company(rules))[0])
    assert job.title == "Staff Platform Engineer"
    assert job.city == "Shenzhen"
    assert job.description == "Kubernetes, Go"
    assert job.apply_url == "https://acme.example.com/jobs/42"  # 没给链接就用页面地址
    assert job.external_id == "42"


# ---------- 合规与"没配规则" ----------


def test_robots_disallowed_stops_before_fetching_pages():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text=ROBOTS_DENY)

    adapter = OfficialSiteAdapter(client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(RobotsDisallowed):
        adapter.discover(make_company({"list_url": "https://acme.example.com/careers"}))
    assert seen == ["https://acme.example.com/robots.txt"]  # 只碰了 robots，没抓页面


def test_missing_rules_raises_not_configured():
    adapter = make_adapter({})
    company = make_company(None)  # 无 html_rules
    with pytest.raises(OfficialSiteNotConfigured):
        adapter.discover(company)


def test_rules_present_but_page_empty_raises_not_configured():
    url = "https://acme.example.com/careers"
    adapter = make_adapter({url: "<html><body>no jobs here</body></html>"})
    with pytest.raises(OfficialSiteNotConfigured):
        adapter.discover(make_company({"list_url": url, "fields": {"title": {"regex": "<h2>(.*?)</h2>"}}}))


def test_normalize_accepts_prebuilt_payload():
    """normalize_raw 与网络层解耦：直接喂 payload 也能出标准化职位（便于单测/回放）。"""
    adapter = make_adapter({})
    raw = RawJob(
        payload={
            "title": "Data Analyst",
            "url": "https://acme.example.com/jobs/77",
            "city": "成都",
            "publish_date": "2026-09-01T08:00:00Z",
            "description": "<p>SQL 与报表</p>",
            "external_id": "77",
        },
        company_slug="acme",
    )
    job = adapter.normalize_raw(raw)
    assert (job.title, job.city, str(job.publish_date)) == ("Data Analyst", "成都", "2026-09-01")
    assert job.description == "SQL 与报表"


def test_registry_still_resolves_official_site_and_netease():
    from app.adapters.netease import NeteaseAdapter

    assert isinstance(get_adapter("official_site"), OfficialSiteAdapter)
    assert isinstance(get_adapter("netease"), NeteaseAdapter)
    assert isinstance(get_adapter("netease"), OfficialSiteAdapter)  # 家族继承关系不变
