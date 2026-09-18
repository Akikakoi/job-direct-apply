"""投递闭环测试（P3 §8）：状态机、合规钩子、防重复、催进扫描、feedback_log。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app, get_session
from app.models import Application, FeedbackLog, Job, Resume
from app.services.applications import IllegalTransition, scan_reminders, transition


def _add_job(session, title: str = "Python 后端") -> Job:
    job = Job(
        external_id=f"test-{title}",
        title=title,
        city="杭州市",
        skills=["python"],
        apply_url=f"https://example.com/{title}",
        source="test",
        status="active",
    )
    session.add(job)
    session.commit()
    return job


def _add_resume(session) -> Resume:
    r = Resume(user_id=1, raw_text="x", profile={"skills": ["python"]}, lang="zh")
    session.add(r)
    session.commit()
    return r


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


# ---------- 状态机 ----------


def test_happy_path_transitions(session):
    job = _add_job(session)
    app_row = Application(user_id=1, job_id=job.id, mode="direct_link", status="submitted")
    session.add(app_row)
    session.commit()

    for next_status in ("under_review", "interview", "offer"):
        app_row = transition(session, app_row, next_status, note=f"-> {next_status}")
    assert app_row.status == "offer"
    # offer 终态：再迁移必须抛错
    with pytest.raises(IllegalTransition):
        transition(session, app_row, "rejected")
    # 回灌链：under_review 不属 outcome 口径（记 None），其余照写
    outcomes = [log.outcome for log in session.query(FeedbackLog).order_by(FeedbackLog.id)]
    assert outcomes == [None, "interview", "offer"]


def test_illegal_skips(session):
    job = _add_job(session)
    app_row = Application(user_id=1, job_id=job.id, status="submitted")
    session.add(app_row)
    session.commit()
    with pytest.raises(IllegalTransition):
        transition(session, app_row, "offer")  # 不能跳过面试


def test_interview_can_go_rejected(session):
    job = _add_job(session)
    app_row = Application(user_id=1, job_id=job.id, status="interview")
    session.add(app_row)
    session.commit()
    app_row = transition(session, app_row, "rejected", note="一面挂")
    assert app_row.status == "rejected"
    log = session.query(FeedbackLog).order_by(FeedbackLog.id.desc()).first()
    assert log.outcome == "rejected" and log.note == "一面挂"


# ---------- API ----------


def test_create_requires_authorized(session, client):
    job = _add_job(session)
    resp = client.post("/api/applications", json={"user_id": 1, "job_id": job.id, "authorized": False})
    assert resp.status_code == 403  # 合规钩子


def test_create_and_list(session, client):
    job = _add_job(session)
    resume = _add_resume(session)
    resp = client.post(
        "/api/applications",
        json={"user_id": 1, "resume_id": resume.id, "job_id": job.id, "authorized": True},
    )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["status"] == "submitted"
    assert body["apply_url"] == job.apply_url

    resp = client.get("/api/applications", params={"user_id": 1, "status": "submitted"})
    assert resp.json()["data"]["total"] == 1


def test_duplicate_application_conflict(session, client):
    job = _add_job(session)
    payload = {"user_id": 1, "job_id": job.id, "authorized": True}
    assert client.post("/api/applications", json=payload).status_code == 200
    assert client.post("/api/applications", json=payload).status_code == 409


def test_status_update_via_api(session, client):
    job = _add_job(session)
    rid = client.post("/api/applications", json={"user_id": 1, "job_id": job.id, "authorized": True}).json()["data"]["id"]

    ok = client.post(f"/api/applications/{rid}/status", json={"status": "under_review"})
    assert ok.json()["data"]["status"] == "under_review"

    bad = client.post(f"/api/applications/{rid}/status", json={"status": "offer"})
    assert bad.status_code == 400  # 跳步非法

    missing = client.post("/api/applications/9999/status", json={"status": "closed"})
    assert missing.status_code == 404


def test_job_must_exist_and_active(session, client):
    job = _add_job(session)
    job.status = "expired"
    session.commit()
    resp = client.post("/api/applications", json={"user_id": 1, "job_id": job.id, "authorized": True})
    assert resp.status_code == 404


# ---------- 催进扫描 ----------


def test_scan_reminders_threshold(session):
    job = _add_job(session)
    stale = Application(user_id=1, job_id=job.id, status="submitted")
    fresh = Application(user_id=2, job_id=job.id, status="submitted")
    interview = Application(user_id=1, job_id=job.id, status="under_review")
    session.add_all([stale, fresh, interview])
    session.commit()

    now = datetime.utcnow()
    stale.updated_at = now - timedelta(days=5)
    fresh.updated_at = now - timedelta(days=1)
    interview.updated_at = now - timedelta(days=4)  # under_review 也纳入催进
    session.commit()

    items = scan_reminders(session, now=now)
    ids = {i["application_id"] for i in items}
    assert ids == {stale.id, interview.id}
    assert all(i["stuck_days"] >= 3 for i in items)  # 默认 T=3

    # 按 user 过滤
    items_user1 = scan_reminders(session, now=now, user_id=1)
    assert {i["application_id"] for i in items_user1} == {stale.id, interview.id}


def test_reminders_api(session, client):
    job = _add_job(session)
    rid = client.post("/api/applications", json={"user_id": 1, "job_id": job.id, "authorized": True}).json()["data"]["id"]
    # 刚创建不催进
    assert client.get("/api/reminders", params={"user_id": 1}).json()["data"]["total"] == 0

    app_row = session.get(Application, rid)
    app_row.updated_at = datetime.utcnow() - timedelta(days=4)
    session.commit()
    body = client.get("/api/reminders", params={"user_id": 1}).json()["data"]
    assert body["total"] == 1
    assert body["items"][0]["job_title"] == job.title
    assert body["items"][0]["stuck_days"] == 4
