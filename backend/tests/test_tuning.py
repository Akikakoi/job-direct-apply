"""权重回归调参测试（§12.7 #3）：网格 / NDCG 评估 / 搜索结论 / API 入参校验。"""

from __future__ import annotations

from app.models import Application, Job, Resume
from app.services.applications import transition
from app.services.tuning import env_snippet, ndcg_at_k, search_weights, simplex_grid


# ---------- 纯函数 ----------


def test_simplex_grid_sums_to_one():
    grid = simplex_grid(0.1)
    assert len(grid) == 286  # C(10+3, 3)
    for w in grid:
        assert abs(sum(w.values()) - 1.0) < 1e-9
        assert set(w) == {"skill", "city", "exp", "role"}


def test_simplex_grid_coarse_step_shrinks():
    grid = simplex_grid(0.5)
    assert len(grid) == 10  # C(2+3, 3)；step 越大组合越少
    assert {"skill": 1.0, "city": 0.0, "exp": 0.0, "role": 0.0} in grid


def test_ndcg_at_k_prefers_right_weight():
    # 两行样本：job 1 只靠 skill 得高分，job 2 只靠 city 得高分；相关的是 job 1
    parts_a = {"skill": 1.0, "city": 0.0, "exp": 0.0, "role": 0.0}
    parts_b = {"skill": 0.0, "city": 1.0, "exp": 0.0, "role": 0.0}
    samples = [{"resume_id": 1, "relevance": {1: 2}, "rows": [(1, parts_a, 0.0), (2, parts_b, 0.0)]}]

    skill_heavy = {"skill": 1.0, "city": 0.0, "exp": 0.0, "role": 0.0}
    city_heavy = {"skill": 0.0, "city": 1.0, "exp": 0.0, "role": 0.0}
    assert ndcg_at_k(samples, skill_heavy, 0.75, 10) == 1.0
    assert ndcg_at_k(samples, city_heavy, 0.75, 10) < 1.0
    # 无有效样本（空列表）返回 None，而非 0
    assert ndcg_at_k([], skill_heavy, 0.75, 10) is None


def test_env_snippet_lines():
    snippet = env_snippet({"skill": 1.0, "city": 0.0, "exp": 0.0, "role": 0.0, "alpha": 0.9})
    assert "MATCH_W_SKILL=1.0" in snippet
    assert "MATCH_ALPHA=0.9" in snippet
    assert len(snippet.splitlines()) == 5


# ---------- 数据侧 ----------


def _seed(session, resumes: int = 3):
    """3 份简历 + 2 个 active 职位，相关职位只在 skill 分项上有优势。

    R（相关，interview=2）：skill=1，city/exp/role=0
    C（干扰）：skill=0.5 中性，city/exp/role=1
    基线权重(0.5/0.2/0.15/0.15)下 C 排在 R 前 → 基线 NDCG < 1；
    网格内 skill=1 的组合能把 R 排到第一 → 有可调空间。
    """
    job_r = Job(external_id="R", title="Python 开发工程师", city="北京",
                experience_min=10, skills=["python"], source="test", status="active",
                apply_url="https://x.com/r")
    job_c = Job(external_id="C", title="后端工程师", city="杭州",
                experience_min=3, skills=[], source="test", status="active",
                apply_url="https://x.com/c")
    session.add_all([job_r, job_c])
    session.commit()

    out = []
    for i in range(resumes):
        resume = Resume(
            user_id=1,
            profile={"skills": ["python"], "cities": ["杭州"],
                     "experience_years": 5, "target_role": "后端"},
        )
        session.add(resume)
        session.commit()
        app = Application(user_id=1, resume_id=resume.id, job_id=job_r.id,
                          apply_url="https://x.com/r", status="submitted")
        session.add(app)
        session.commit()
        transition(session, app, "under_review")
        transition(session, app, "interview")
        out.append((resume, app))
    return job_r, job_c, out


def test_search_weights_finds_better_than_baseline(session):
    _seed(session)
    report = search_weights(session, k=10, step=0.1)

    assert report["samples"] == {"resumes": 3, "jobs": 2}
    assert report["baseline"]["ndcg_at_k"] < 1.0
    assert report["grid"]["combos"] > 0

    # 候选按 NDCG 降序
    scores = [c["ndcg_at_k"] for c in report["candidates"]]
    assert scores == sorted(scores, reverse=True)

    best = report["best"]
    assert best is not None
    assert best["ndcg_at_k"] == 1.0
    assert best["gain"] > 0
    assert "MATCH_W_SKILL" in best["env_snippet"]
    assert set(best["weights"]) == {"skill", "city", "exp", "role", "alpha", "beta"}


def test_search_weights_ignores_zero_relevance_resume(session):
    job_r, job_c, _ = _seed(session)
    # 第 4 份简历只有 rejected 反馈（相关度 0）→ 不进样本
    resume = Resume(user_id=1, profile={"skills": ["python"]})
    session.add(resume)
    session.commit()
    app = Application(user_id=1, resume_id=resume.id, job_id=job_c.id,
                      apply_url="https://x.com/c", status="submitted")
    session.add(app)
    session.commit()
    transition(session, app, "rejected")

    report = search_weights(session, k=10, step=0.1)
    assert report["samples"]["resumes"] == 3


def test_search_weights_no_samples_returns_hint(session):
    report = search_weights(session)
    assert report["samples"]["resumes"] == 0
    assert report["best"] is None
    assert report["baseline"]["ndcg_at_k"] is None
    assert report["notes"] and "暂不建议调参" in report["notes"][0]


# ---------- API ----------


def test_tuning_endpoint(session, client):
    _seed(session)
    resp = client.get("/api/insights/tuning", params={"step": 0.1})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["samples"]["resumes"] == 3
    assert data["best"]["ndcg_at_k"] == 1.0
    assert data["baseline"]["weights"]["skill"] == 0.5  # 只读，未改配置


def test_tuning_endpoint_step_out_of_range(session, client):
    for step in (0.01, 0.6):
        resp = client.get("/api/insights/tuning", params={"step": step})
        assert resp.status_code == 422