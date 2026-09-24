"""面试陪伴闭环测试（§12.6 P5 ②）：陪伴包 / 面试时间登记 / 面试催进扫描。"""

from __future__ import annotations

from datetime import datetime, timedelta
from itertools import count

from app.models import Application, Company, Job, Resume
from app.services.interview import (
    _days_left,
    build_interview_kit,
    scan_interview_reminders,
    set_interview_at,
)

NOW = datetime(2026, 9, 24, 9, 0)

# 单条测试内会多次 _seed；companies.slug 与 (company_id, external_id) 均有唯一约束
_SEQ = count(1)


def _seed(session, job_skills=None, resume_skills=None, status="interview", interview_at=None,
          company=True, title="后端开发工程师", description="负责服务端开发，要求熟悉 Python。"):
    n = next(_SEQ)
    if company:
        comp = Company(slug=f"acme-{n}", name="Acme", ats_type="greenhouse",
                       site_url="https://acme.com/jobs")
        session.add(comp)
        session.commit()
    else:
        comp = None
    job = Job(
        company_id=comp.id if comp else None,
        external_id=f"J{n}",
        title=title,
        description=description,
        city="杭州",
        skills=job_skills if job_skills is not None else ["Python", "K8s"],
        apply_url="https://acme.com/apply/1",
        source="greenhouse",
        status="active",
    )
    session.add(job)
    resume = Resume(user_id=1, profile={"skills": resume_skills if resume_skills is not None else ["python"]})
    session.add(resume)
    session.commit()
    app = Application(
        user_id=1, resume_id=resume.id, job_id=job.id, status=status,
        apply_url=job.apply_url, interview_at=interview_at,
    )
    session.add(app)
    session.commit()
    return comp, job, resume, app


def test_days_left_helper():
    assert _days_left(datetime(2026, 9, 26, 10, 0), NOW) == 2
    assert _days_left(datetime(2026, 9, 23, 10, 0), NOW) == -1
    assert _days_left(None, NOW) is None


def test_kit_structure_gaps_and_company_pack(session):
    comp, job, resume, app = _seed(session)
    session.add(Job(company_id=comp.id, external_id="J2", title="另一职位", apply_url="https://x/2",
                    source="greenhouse", status="active"))
    session.add(Job(company_id=comp.id, external_id="J3", title="已过期职位", apply_url="https://x/3",
                    source="greenhouse", status="expired"))
    session.commit()

    kit = build_interview_kit(session, app, now=NOW)

    assert kit["application"]["job_title"] == "后端开发工程师"
    assert kit["application"]["days_left"] is None  # 未登记面试时间
    # 技能命中/缺口：两侧均小写比对，K8s 未在简历里 → 缺口
    assert kit["strengths"] == ["Python"]
    assert kit["gaps"] == ["K8s"]
    points = {p["skill"]: p for p in kit["skill_points"]}
    assert points["Python"]["have"] is True
    assert points["K8s"]["have"] is False
    assert "诚实口径" in points["K8s"]["hint"]
    # 清单必含材料项与缺口项
    keys = {c["key"] for c in kit["checklist"]}
    assert {"resume_aligned", "project_stories", "gap_sprint", "logistics", "questions"} <= keys
    assert next(c for c in kit["checklist"] if c["key"] == "gap_sprint")["done"] is False
    assert kit["english_interview"] is False
    assert len(kit["reverse_questions"]) >= 3
    # 背景包只统计该公司 active 职位（expired 不计）
    assert kit["company_pack"]["name"] == "Acme"
    assert kit["company_pack"]["open_jobs"] == 2
    assert any("在招职位" in p for p in kit["company_pack"]["talking_points"])


def test_gap_sprint_done_when_no_gap(session):
    _, _, _, app = _seed(session, job_skills=["Python", "SQL"], resume_skills=["python", "sql"])
    kit = build_interview_kit(session, app, now=NOW)
    assert kit["gaps"] == []
    assert next(c for c in kit["checklist"] if c["key"] == "gap_sprint")["done"] is True


def test_english_job_adds_language_checklist(session):
    _, _, _, app = _seed(
        session,
        title="Senior Backend Engineer",
        description="We are hiring a backend engineer to build payment services with Python and Kubernetes. "
                    "You will own the design, rollout and reliability of the platform.",
    )
    kit = build_interview_kit(session, app, now=NOW)
    assert kit["english_interview"] is True
    assert "english_self_intro" in {c["key"] for c in kit["checklist"]}


def test_kit_without_job_or_resume(session):
    app = Application(user_id=1, job_id=None, resume_id=None, status="submitted")
    session.add(app)
    session.commit()
    kit = build_interview_kit(session, app, now=NOW)
    assert kit["skill_points"] == []
    assert kit["company_pack"]["name"] is None
    assert kit["company_pack"]["open_jobs"] == 0
    # 无技能标签时显式提示（避免前端把空清单当成"无需准备"）
    assert any("无 skills 标签" in n for n in kit["notes"])


def test_set_interview_at_rejects_terminal(session):
    _, _, _, app = _seed(session, status="offer")
    try:
        set_interview_at(session, app, NOW)
    except ValueError as exc:
        assert "终态" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("终态应拒绝登记面试时间")


def test_scan_interview_reminders(session):
    _, _, _, soon = _seed(session, interview_at=NOW + timedelta(days=1))
    _, _, _, other = _seed(session, interview_at=NOW + timedelta(days=10))
    _, _, _, stale = _seed(session, interview_at=NOW - timedelta(days=30))
    _, _, _, no_time = _seed(session, interview_at=None)
    _, _, _, not_interview = _seed(session, status="under_review", interview_at=NOW + timedelta(days=1))
    # 已过 1 天但未更新结果 → 仍在宽限窗口内，且标 overdue
    _, _, _, overdue = _seed(session, interview_at=NOW - timedelta(days=1))

    items = scan_interview_reminders(session, now=NOW, within_days=2)
    ids = [it["application_id"] for it in items]
    assert soon.id in ids and overdue.id in ids
    assert other.id not in ids  # 10 天后，超出前瞻窗口
    assert stale.id not in ids  # 超 7 天宽限，不再打扰
    assert no_time.id not in ids
    assert not_interview.id not in ids

    by_id = {it["application_id"]: it for it in items}
    assert by_id[soon.id]["days_left"] == 1
    assert by_id[soon.id]["overdue"] is False
    assert by_id[overdue.id]["overdue"] is True

    # 用户过滤
    assert scan_interview_reminders(session, now=NOW, within_days=2, user_id=999) == []


def test_interview_api_kit_and_interview_at(session, client):
    _, _, _, app = _seed(session, interview_at=None)

    resp = client.get(f"/api/applications/{app.id}/interview-kit")
    assert resp.status_code == 200
    assert resp.json()["data"]["gaps"] == ["K8s"]

    assert client.get("/api/applications/9999/interview-kit").status_code == 404

    # 登记面试时间（ISO 字符串）→ 进入催进扫描
    when = (datetime.utcnow() + timedelta(days=1)).replace(microsecond=0)
    resp = client.put(f"/api/applications/{app.id}/interview-at", json={"interview_at": when.isoformat()})
    assert resp.status_code == 200
    assert resp.json()["data"]["interview_at"] is not None
    assert client.get("/api/interview-reminders").json()["data"]["total"] == 1

    # 清空
    resp = client.put(f"/api/applications/{app.id}/interview-at", json={"interview_at": None})
    assert resp.status_code == 200
    assert resp.json()["data"]["interview_at"] is None
    assert client.get("/api/interview-reminders").json()["data"]["total"] == 0

    # 非法时间格式 → 422（Pydantic 校验）；终态 → 400
    assert client.put(f"/api/applications/{app.id}/interview-at", json={"interview_at": "明天"}).status_code == 422
    _, _, _, done = _seed(session, status="rejected")
    assert client.put(f"/api/applications/{done.id}/interview-at",
                      json={"interview_at": when.isoformat()}).status_code == 400


def test_interview_reminder_task_run(session, monkeypatch, capsys):
    """beat 任务直调：无邮件/IM 配置时降级打日志，不抛异常。"""
    from app.workers import celery_app as mod

    _seed(session, interview_at=datetime.utcnow() + timedelta(days=1))
    monkeypatch.setattr("app.core.db.SessionLocal", lambda: session)
    result = mod.interview_reminder_task.run()
    assert result == {"total": 1, "mailed": False, "im_sent": False}
    assert "[interview]" in capsys.readouterr().out

    # 无命中时返回零值
    session.query(Application).delete()
    session.commit()
    assert mod.interview_reminder_task.run() == {"total": 0, "mailed": False, "im_sent": False}