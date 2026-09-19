"""质量基线测试：NDCG 纯函数 + /api/insights/quality 汇总口径。"""

from __future__ import annotations

from app.models import Application, Job, MatchScore, Resume
from app.services.insights import build_quality_report, ndcg


def test_ndcg_perfect_order():
    assert ndcg([3, 2, 0]) == 1.0
    assert ndcg([3, 2]) == 1.0


def test_ndcg_imperfect_order():
    # 倒序排列应显著低于 1；手算校验
    score = ndcg([0, 2, 3])
    assert 0 < score < 1
    # 手算：dcg = 2/√3 + 3/2；idcg = 3/√2 + 2/√3
    expected = (2 / 3**0.5 + 3 / 2) / (3 / 2**0.5 + 2 / 3**0.5)
    assert abs(score - expected) < 1e-3


def test_ndcg_all_zero_relevance():
    assert ndcg([0, 0, 0]) == 0.0
    assert ndcg([]) == 0.0


def _seed(session):
    """1 份简历 + 3 个职位 + 3 条投递（interview/rejected/未反馈）。"""
    resume = Resume(user_id=1, profile={"skills": ["python"]})
    session.add(resume)
    session.commit()
    jobs = []
    for i in range(3):
        job = Job(external_id=f"J{i}", title=f"Job{i}", apply_url=f"https://x.com/{i}",
                  source="test", status="active")
        session.add(job)
        jobs.append(job)
    session.commit()

    # 打分：J0=0.9, J1=0.5；J2 无打分（考察关联缺失的鲁棒性）
    session.add(MatchScore(resume_id=resume.id, job_id=jobs[0].id, rule_score=0.9, final_score=0.9))
    session.add(MatchScore(resume_id=resume.id, job_id=jobs[1].id, rule_score=0.5, final_score=0.5))
    apps = []
    for i in range(3):
        app = Application(user_id=1, resume_id=resume.id, job_id=jobs[i].id,
                          apply_url=f"https://x.com/{i}", status="submitted")
        session.add(app)
        apps.append(app)
    session.commit()
    return resume, jobs, apps


def test_report_metrics(session, client):
    from app.services.applications import transition

    resume, jobs, apps = _seed(session)
    # 状态迁移写 feedback_log：A0 走 submitted→under_review→interview；
    # A1 走 submitted→rejected（A2 保持 submitted 无反馈）
    transition(session, apps[0], "under_review")
    transition(session, apps[0], "interview")
    transition(session, apps[1], "rejected")

    resp = client.get("/api/insights/quality")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total_applications"] == 3
    assert data["outcome_counts"] == {"interview": 1, "rejected": 1}
    # 覆盖 2/3；命中率 1/2（no_feedback 口径：submitted 无 outcome 不计分母）
    assert data["feedback_coverage"] == 0.6667
    assert data["hit_rate"] == 0.5
    # 分数对照：interview 组均值 0.9（分高→结果好 的可验证口径）
    assert data["avg_final_score_by_outcome"] == {"interview": 0.9, "rejected": 0.5}
    # NDCG：唯一有反馈简历，J0(interview=2) 排第一 → 1.0
    assert data["ndcg_at_k"] == 1.0
    assert data["ndcg_resumes"] == 1


def test_report_user_scope_and_empty(session, client):
    _seed(session)
    # 空库口径：全 None 不抛错
    data = client.get("/api/insights/quality", params={"user_id": 42}).json()["data"]
    assert data["total_applications"] == 0
    assert data["hit_rate"] is None
    assert data["ndcg_at_k"] is None
