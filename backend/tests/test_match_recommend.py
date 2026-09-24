"""规则匹配引擎 + /recommend 测试（P2 §7）。全部离线。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app, get_session
from app.models import Job, MatchScore, Resume
from app.pipelines.match import compute_match, refresh_matches


PROFILE = {
    "skills": ["python", "fastapi", "kubernetes"],
    "experience_years": 8,
    "target_role": "高级后端工程师",
    "cities": ["杭州", "上海"],
}


def _add_job(session, title: str, city: str | None, skills: list, exp_min: int | None = None) -> Job:
    job = Job(
        external_id=f"test-{title}",
        title=title,
        city=city,
        skills=skills,
        experience_min=exp_min,
        apply_url=f"https://example.com/{title}",
        source="test",
        status="active",
    )
    session.add(job)
    session.commit()
    return job


def _add_resume(session, profile: dict) -> Resume:
    r = Resume(user_id=1, raw_text="x", profile=profile, lang="zh")
    session.add(r)
    session.commit()
    return r


# ---------- 分项打分 ----------


def test_skill_hit_and_explain(session):
    job = _add_job(session, "Python 后端", "杭州市", ["python", "fastapi", "kafka"])
    result = compute_match(PROFILE, job)
    skill = next(e for e in result["explain"] if e["key"] == "skill")
    assert skill["hit"] == ["fastapi", "python"]  # kafka 缺失
    assert skill["missing"] == ["kafka"]
    assert skill["score"] == pytest.approx(0.6667)  # compute_match 内 round 4 位


def test_skill_unknown_neutral(session):
    job = _add_job(session, "运维", "杭州市", [])
    result = compute_match(PROFILE, job)
    skill = next(e for e in result["explain"] if e["key"] == "skill")
    assert skill["score"] == 0.5 and "skills_unknown_neutral" in skill["note"]


def test_city_fit_contains_and_remote(session):
    job_hz = _add_job(session, "a", "杭州市", ["python"])
    assert compute_match(PROFILE, job_hz)["explain"][1]["score"] == 1.0  # 杭州 ⊂ 杭州市

    job_multi = _add_job(session, "b", "Menlo Park, CA; 上海", ["python"])
    assert compute_match(PROFILE, job_multi)["explain"][1]["score"] == 1.0

    job_remote = _add_job(session, "c", "US-Remote", ["python"])
    assert compute_match(PROFILE, job_remote)["explain"][1]["score"] == 0.8

    job_na = _add_job(session, "d", "N/A", ["python"])
    assert compute_match(PROFILE, job_na)["explain"][1]["score"] == 0.5

    job_other = _add_job(session, "e", "乌鲁木齐", ["python"])
    assert compute_match(PROFILE, job_other)["explain"][1]["score"] == 0.0


def test_city_fit_cross_region_neutral(session):
    """§12.7 #9 C：跨区城市"不可比" → 0.5 中性，不再系统性压低海外。"""
    job_sf = _add_job(session, "a", "San Francisco, CA", ["python"])
    city = next(e for e in compute_match(PROFILE, job_sf)["explain"] if e["key"] == "city")
    assert city["score"] == 0.5 and city["note"] == "cross_region_neutral"

    # 反向对称：海外意向城市 vs 国内职位 → 同样中性
    overseas_profile = dict(PROFILE, cities=["New York, NY"])
    job_hz = _add_job(session, "b", "杭州", ["python"])
    city2 = next(e for e in compute_match(overseas_profile, job_hz)["explain"] if e["key"] == "city")
    assert city2["score"] == 0.5 and city2["note"] == "cross_region_neutral"

    # 同区不同城市仍是 0（明确不匹配），不被"跨区中性"放宽
    job_wlmq = _add_job(session, "c", "乌鲁木齐", ["python"])
    assert compute_match(PROFILE, job_wlmq)["explain"][1]["score"] == 0.0


def test_role_fit_bilingual(session):
    """§12.7 #9 C：中英双向 role 同义词，跨语言 title 也能命中。"""
    # 中文 target_role（后端）→ 英文 title
    en_title = _add_job(session, "Senior Backend Engineer, Payments", "San Francisco, CA", ["python"])
    assert compute_match(PROFILE, en_title)["explain"][3]["score"] == 1.0

    # 英文 target_role → 中文 title（反查中文核心词）；ASCII 后缀"Engineer"需剥净
    en_profile = dict(PROFILE, target_role="Backend Engineer")
    zh_title = _add_job(session, "后端开发工程师（Java）", "杭州", ["python"])
    role = next(e for e in compute_match(en_profile, zh_title)["explain"] if e["key"] == "role")
    assert role["score"] == 1.0 and role["core"] == "后端"

    # 英文 ↔ 英文：剥后缀后核心词命中（Backend Engineer vs Backend Developer）
    en_dev = _add_job(session, "Backend Developer (Go)", "San Francisco, CA", ["python"])
    assert compute_match(en_profile, en_dev)["explain"][3]["score"] == 1.0


def test_exp_fit_ladder(session):
    ok = _add_job(session, "a", "杭州市", ["python"], exp_min=5)
    assert compute_match(PROFILE, ok)["explain"][2]["score"] == 1.0

    slightly = _add_job(session, "b", "杭州市", ["python"], exp_min=9)
    assert compute_match(PROFILE, slightly)["explain"][2]["score"] == 0.5

    below = _add_job(session, "c", "杭州市", ["python"], exp_min=15)
    assert compute_match(PROFILE, below)["explain"][2]["score"] == 0.0

    no_req = _add_job(session, "d", "杭州市", ["python"], exp_min=None)
    assert compute_match(PROFILE, no_req)["explain"][2]["score"] == 1.0

    no_years = compute_match({"skills": ["python"]}, ok)
    assert no_years["explain"][2]["score"] == 0.5


def test_role_fit_strips_modifiers(session):
    hit = _add_job(session, "后端工程师（Java 方向）", "杭州市", ["python"])
    assert compute_match(PROFILE, hit)["explain"][3]["score"] == 1.0  # "高级"剥掉后命中

    # 核心词匹配："后端工程师"剥掉"工程师"→"后端"，命中"后端开发工程师"
    dev = _add_job(session, "后端开发工程师（Go）", "杭州市", ["python"])
    assert compute_match(PROFILE, dev)["explain"][3]["score"] == 1.0

    # 同义词："后端" ≈ "服务端"
    srv = _add_job(session, "服务端开发工程师（射击项目）", "杭州市", ["python"])
    assert compute_match(PROFILE, srv)["explain"][3]["score"] == 1.0

    miss = _add_job(session, "产品经理", "杭州市", ["python"])
    assert compute_match(PROFILE, miss)["explain"][3]["score"] == 0.0

    no_role = compute_match({"skills": ["python"]}, hit)
    assert no_role["explain"][3]["score"] == 0.5


def test_score_weighted_sum(session):
    job = _add_job(session, "后端工程师", "杭州市", ["python", "fastapi"])
    result = compute_match(PROFILE, job)
    # skill 2/2=1 * 0.5 + city 1 * 0.2 + exp 1 * 0.15 + role 1 * 0.15
    assert result["score"] == pytest.approx(1.0)


# ---------- refresh_matches + /recommend API ----------


@pytest.fixture()
def client(session):
    def override_get_session():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app)
    app.dependency_overrides.pop(get_session, None)


def test_refresh_matches_idempotent(session):
    _add_job(session, "Python 后端", "杭州市", ["python"])
    _add_job(session, "另一家", "上海市", ["go"])
    resume = _add_resume(session, PROFILE)

    n1 = refresh_matches(session, resume)
    n2 = refresh_matches(session, resume)
    assert n1 == n2 == 2
    assert session.query(MatchScore).count() == 2


def test_recommend_sorted_with_explain(session, client):
    good = _add_job(session, "后端工程师", "杭州市", ["python", "fastapi", "kubernetes"])
    _add_job(session, "前端工程师", "北京市", ["javascript"])
    resume = _add_resume(session, PROFILE)

    resp = client.get("/api/recommend", params={"resume_id": resume.id})
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["total"] == 2
    assert body["items"][0]["job_id"] == good.id  # 满分项排第一
    assert body["items"][0]["score"] >= body["items"][1]["score"]
    assert {"skill", "city", "exp", "role", "semantic"} <= {
        e["key"] for e in body["items"][0]["explain"]
    }  # P4：融合分后 explain 含 semantic 分量


def test_profile_update_triggers_rematch(session, client):
    _add_job(session, "后端工程师", "杭州市", ["python"])
    resume = _add_resume(session, PROFILE)

    # 首次 recommend 生成缓存
    client.get("/api/recommend", params={"resume_id": resume.id})
    assert session.query(MatchScore).count() == 1

    # 人工修正 target_role → 触发重算（source=manual）
    upd = client.put(
        f"/api/resumes/{resume.id}/profile",
        json={"profile": {"target_role": "前端工程师"}},
    )
    assert upd.status_code == 200
    assert upd.json()["data"]["matches_refreshed"] == 1

    resp = client.get("/api/recommend", params={"resume_id": resume.id})
    role = next(e for e in resp.json()["data"]["items"][0]["explain"] if e["key"] == "role")
    assert role["score"] == 0.0  # 新 target_role 与后端职位不再命中


def test_recommend_404(session, client):
    resp = client.get("/api/recommend", params={"resume_id": 9999})
    assert resp.status_code == 404
