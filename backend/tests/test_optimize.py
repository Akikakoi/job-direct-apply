"""AI 简历优化测试（§12.6 P5 ③）：逐条差距 / 改写建议 / 关键词覆盖 / LLM 兜底 / 接口。"""

from __future__ import annotations

from itertools import count

from app.models import Job, Resume
from app.services.optimize import build_optimization, degree_rank, keyword_coverage

# 唯一约束 (company_id, external_id)：company_id 均为 None，靠自增序号避免撞车
_SEQ = count(1)


def _seed(session, profile=None, raw_text=None, job_skills=None, experience_min=None,
          degree_req=None, city="杭州", title="后端开发工程师",
          description="负责服务端开发，要求熟悉 Python。"):
    n = next(_SEQ)
    job = Job(
        external_id=f"OPT{n}",
        title=title,
        description=description,
        city=city,
        skills=job_skills if job_skills is not None else ["Python", "K8s"],
        experience_min=experience_min,
        degree_req=degree_req,
        apply_url="https://acme.com/apply/1",
        source="greenhouse",
        status="active",
    )
    session.add(job)
    resume = Resume(
        user_id=1,
        profile=profile if profile is not None else {"skills": ["python"]},
        raw_text=raw_text,
    )
    session.add(resume)
    session.commit()
    return resume, job


def test_degree_rank_helper():
    assert degree_rank("phd") > degree_rank("master") > degree_rank("bachelor") > degree_rank("associate")
    assert degree_rank(None) is None
    assert degree_rank("unknown") is None


def test_skill_gaps_and_matched(session):
    resume, job = _seed(session, profile={"skills": ["python"]})
    report = build_optimization(resume, job)
    assert report["matched_skills"] == ["Python"]
    skill_gaps = [g for g in report["gaps"] if g["kind"] == "skill"]
    assert [g["title"] for g in skill_gaps] == ["缺少职位要求的技能：K8s"]
    assert "不要写进简历" in skill_gaps[0]["advice"]


def test_skill_unknown_when_no_resume_skills(session):
    """简历无 skills → 与 match 中性口径同源，不列差距（避免误报）。"""
    resume, job = _seed(session, profile={"skills": []})
    report = build_optimization(resume, job)
    assert [g for g in report["gaps"] if g["kind"] == "skill"] == []
    assert report["matched_skills"] == []


def test_experience_partial_and_below(session):
    # 差 2 年 → partial
    resume, job = _seed(session, profile={"skills": ["python"], "experience_years": 3}, experience_min=5)
    report = build_optimization(resume, job)
    exp = next(g for g in report["gaps"] if g["kind"] == "experience")
    assert exp["status"] == "partial"
    assert "3 年" in exp["title"] and "5 年" in exp["title"]

    # 差 4 年 → miss
    resume2, job2 = _seed(session, profile={"skills": ["python"], "experience_years": 1}, experience_min=5)
    report2 = build_optimization(resume2, job2)
    assert next(g for g in report2["gaps"] if g["kind"] == "experience")["status"] == "miss"

    # 满足要求 / 职位无要求 → 不列差距
    resume3, job3 = _seed(session, profile={"skills": ["python"], "experience_years": 6}, experience_min=5)
    assert not [g for g in build_optimization(resume3, job3)["gaps"] if g["kind"] == "experience"]
    resume4, job4 = _seed(session, profile={"skills": ["python"], "experience_years": 1})
    assert not [g for g in build_optimization(resume4, job4)["gaps"] if g["kind"] == "experience"]


def test_experience_unknown_when_no_years(session):
    resume, job = _seed(session, profile={"skills": ["python"]}, experience_min=3)
    exp = next(g for g in build_optimization(resume, job)["gaps"] if g["kind"] == "experience")
    assert exp["status"] == "unknown"


def test_education_gap(session):
    # 本科 vs 硕士要求 → miss
    resume, job = _seed(session, profile={"skills": ["python"], "edu_degree": "bachelor"}, degree_req="master")
    edu = next(g for g in build_optimization(resume, job)["gaps"] if g["kind"] == "education")
    assert edu["status"] == "miss"
    assert "如实填写" in edu["advice"]

    # 学历未知 → unknown
    resume2, job2 = _seed(session, profile={"skills": ["python"]}, degree_req="master")
    assert next(g for g in build_optimization(resume2, job2)["gaps"] if g["kind"] == "education")["status"] == "unknown"

    # 满足 / na / 空 → 不列差距
    resume3, job3 = _seed(session, profile={"skills": ["python"], "edu_degree": "phd"}, degree_req="bachelor")
    assert not [g for g in build_optimization(resume3, job3)["gaps"] if g["kind"] == "education"]
    resume4, job4 = _seed(session, profile={"skills": ["python"], "edu_degree": "bachelor"}, degree_req="na")
    assert not [g for g in build_optimization(resume4, job4)["gaps"] if g["kind"] == "education"]


def test_city_gap_and_cross_region_skipped(session):
    # 同区不同城市（杭州意向 vs 北京职位）→ miss
    resume, job = _seed(session, profile={"skills": ["python"], "cities": ["杭州"]}, city="北京")
    city_gap = next(g for g in build_optimization(resume, job)["gaps"] if g["kind"] == "city")
    assert city_gap["status"] == "miss"

    # 命中（杭州 ⊂ 杭州市）与「远程」→ 不列差距
    resume2, job2 = _seed(session, profile={"skills": ["python"], "cities": ["杭州"]}, city="杭州市")
    assert not [g for g in build_optimization(resume2, job2)["gaps"] if g["kind"] == "city"]
    resume3, job3 = _seed(session, profile={"skills": ["python"], "cities": ["杭州"]}, city="Remote - APAC")
    assert not [g for g in build_optimization(resume3, job3)["gaps"] if g["kind"] == "city"]

    # 跨区（国内意向 vs 海外职位）→ 中性口径跳过，不误报
    resume4, job4 = _seed(session, profile={"skills": ["python"], "cities": ["杭州"]}, city="New York, NY")
    assert not [g for g in build_optimization(resume4, job4)["gaps"] if g["kind"] == "city"]

    # 简历未填城市 → unknown
    resume5, job5 = _seed(session, profile={"skills": ["python"]}, city="北京")
    assert next(g for g in build_optimization(resume5, job5)["gaps"] if g["kind"] == "city")["status"] == "unknown"


def test_keyword_coverage_and_missing(session):
    desc = "We need strong Kubernetes and Terraform skills to run the payment platform."
    resume, job = _seed(
        session,
        profile={"skills": ["python"]},
        raw_text="熟悉 Python 后端开发，负责服务端接口。",
        job_skills=["Python"],
        description=desc,
    )
    cov = keyword_coverage({"skills": ["python"]}, job, resume.raw_text)
    assert cov["jd_terms"] >= 2
    assert "kubernetes" in cov["missing"] and "terraform" in cov["missing"]
    assert 0 < cov["ratio"] < 1

    report = build_optimization(resume, job)
    kw = next(g for g in report["gaps"] if g["kind"] == "keyword")
    assert "kubernetes" in kw["title"]
    assert "堆砌" in kw["advice"]


def test_keyword_no_terms_when_jd_empty(session):
    resume, job = _seed(session, profile={"skills": ["python"]}, job_skills=[], description="   ", title="??")
    cov = keyword_coverage({"skills": ["python"]}, job, resume.raw_text)
    assert cov["jd_terms"] == 0 and cov["ratio"] is None
    assert not [g for g in build_optimization(resume, job)["gaps"] if g["kind"] == "keyword"]
    # 无 JD 正文 / 无 skills 时显式提示（避免前端把空块当成"无问题"）
    notes = build_optimization(resume, job)["notes"]
    assert any("无正文" in n for n in notes)
    assert any("无 skills 标签" in n for n in notes)


def test_notes_declare_honesty(session):
    resume, job = _seed(session, profile={"skills": ["python"]})
    notes = build_optimization(resume, job)["notes"]
    assert any("不替用户编造" in n for n in notes)
    # 建议里不允许出现任何"编造/虚构经历"的措辞
    for gap in build_optimization(resume, job)["gaps"]:
        assert "编造" not in gap["advice"]
        assert "虚构" not in gap["advice"]


def test_llm_disabled_by_default_and_not_configured(session, monkeypatch):
    resume, job = _seed(session, profile={"skills": ["python"]})

    # 默认关：不触碰 LLM
    report = build_optimization(resume, job)
    assert report["llm"] == {"requested": False, "status": "disabled", "summary": None, "suggestions": []}

    # 开启但未配 key（测试环境 llm_api_key 为空）→ 标注 not_configured，规则结果照常
    monkeypatch.setattr("app.core.config.settings.llm_api_key", "")
    report2 = build_optimization(resume, job, use_llm=True)
    assert report2["llm"]["status"] == "not_configured"
    assert report2["gaps"]  # 规则差距仍在
    assert any("llm_api_key 未配置" in n for n in report2["notes"])


def test_llm_failure_is_graceful(session, monkeypatch):
    resume, job = _seed(session, profile={"skills": ["python"]})

    def _boom(profile, job):  # noqa: ARG001
        raise RuntimeError("bad gateway")

    monkeypatch.setattr("app.services.optimize._call_llm", _boom)
    report = build_optimization(resume, job, use_llm=True)
    assert report["llm"]["status"] == "failed"
    assert "bad gateway" in report["llm"]["error"]
    assert any("LLM 兜底调用失败" in n for n in report["notes"])


def test_llm_ok_path(session, monkeypatch):
    resume, job = _seed(session, profile={"skills": ["python"]})
    monkeypatch.setattr(
        "app.services.optimize._call_llm",
        lambda profile, job: {"summary": "整体匹配", "suggestions": ["把量化结果前置"]},
    )
    report = build_optimization(resume, job, use_llm=True)
    assert report["llm"]["status"] == "ok"
    assert report["llm"]["suggestions"] == ["把量化结果前置"]


def test_optimize_api_endpoints(session, client):
    resume, job = _seed(session, profile={"skills": ["python"]})

    resp = client.post(f"/api/resumes/{resume.id}/optimize", json={"job_id": job.id})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["target"]["job_id"] == job.id
    assert data["llm"]["status"] == "disabled"

    # 简历/职位不存在 → 404
    assert client.post("/api/resumes/9999/optimize", json={"job_id": job.id}).status_code == 404
    assert client.post(f"/api/resumes/{resume.id}/optimize", json={"job_id": 9999}).status_code == 404
    # 缺 job_id → 422（Pydantic 校验）
    assert client.post(f"/api/resumes/{resume.id}/optimize", json={}).status_code == 422