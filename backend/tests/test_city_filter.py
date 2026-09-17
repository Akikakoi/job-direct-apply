"""city 规范化测试：变体展开单测 + /api/jobs 过滤 API 测试。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app, get_session
from app.models import Job
from app.services.city import city_match_variants


# ---------- 单元测试：city_match_variants ----------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("杭州", ["杭州"]),
        ("杭州市", ["杭州"]),  # 去"市"后缀
        ("北京", ["北京"]),
        ("NYC", ["new york", "nyc"]),  # 别名展开 + 原词保留（可命中 "NYC-Privy"）
        ("nyc", ["new york", "nyc"]),  # 大小写不敏感
        (" Bangalore ", ["bangalore", "bengaluru"]),  # 别名 + 去空白
        ("New York", ["new york"]),
        (None, []),
        ("", []),
        ("   ", []),
    ],
)
def test_city_match_variants(query, expected):
    assert city_match_variants(query) == expected


def test_city_match_variants_escapes_like_wildcards():
    # 用户输入 % _ 不应被当作通配符
    variants = city_match_variants("100%市")
    assert variants == ["100\\%"]


# ---------- API 测试：/api/jobs?city= ----------


@pytest.fixture()
def client(session):
    """覆盖依赖的 TestClient，使用内存库。"""

    def override_get_session():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app)
    app.dependency_overrides.pop(get_session, None)


def _add_job(session, title: str, city: str | None) -> None:
    session.add(
        Job(
            external_id=f"test-{title}",
            title=title,
            city=city,
            apply_url=f"https://example.com/{title}",
            source="test",
            status="active",
        )
    )
    session.commit()


def test_city_chinese_suffix_stripped(session, client):
    _add_job(session, "后端工程师", "杭州市")
    _add_job(session, "前端工程师", "北京市")

    resp = client.get("/api/jobs", params={"city": "杭州"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["total"] == 1
    assert body["data"]["items"][0]["title"] == "后端工程师"

    # 带后缀查询同样命中
    resp = client.get("/api/jobs", params={"city": "杭州市"})
    assert resp.json()["data"]["total"] == 1


def test_city_multi_location_and_state(session, client):
    _add_job(session, "ny job", "New York, NY")
    _add_job(session, "multi job", "Menlo Park, CA; New York, NY")
    _add_job(session, "sf job", "San Francisco, CA")

    resp = client.get("/api/jobs", params={"city": "New York"})
    body = resp.json()
    assert body["data"]["total"] == 2
    titles = {i["title"] for i in body["data"]["items"]}
    assert titles == {"ny job", "multi job"}


def test_city_alias(session, client):
    _add_job(session, "bengaluru job", "Bengaluru")
    _add_job(session, "ny job", "New York")

    resp = client.get("/api/jobs", params={"city": "Bangalore"})
    assert resp.json()["data"]["total"] == 1

    resp = client.get("/api/jobs", params={"city": "NYC"})
    assert resp.json()["data"]["total"] == 1
    assert resp.json()["data"]["items"][0]["title"] == "ny job"


def test_city_no_match(session, client):
    _add_job(session, "hz job", "杭州市")
    resp = client.get("/api/jobs", params={"city": "乌鲁木齐"})
    assert resp.json()["data"]["total"] == 0


def test_city_combined_with_source(session, client):
    _add_job(session, "hz a", "杭州市")
    session.add(
        Job(
            external_id="test-hz-b",
            title="hz b",
            city="杭州市",
            apply_url="https://example.com/b",
            source="other",
            status="active",
        )
    )
    session.commit()

    resp = client.get("/api/jobs", params={"city": "杭州", "source": "test"})
    body = resp.json()
    assert body["data"]["total"] == 1
    assert body["data"]["items"][0]["title"] == "hz a"
