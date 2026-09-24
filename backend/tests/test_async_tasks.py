"""§9 后台任务异步化测试：全局 TTL 下架、上传后异步解析、画像修改后异步重算。

全部离线：任务以 `.run()` 直调（SessionLocal 指向内存库），API 侧投递用
monkeypatch 替换 `app.main._enqueue`，既不连 broker 也不触网。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

celery = pytest.importorskip("celery")

from app.main import app, get_session  # noqa: E402
from app.models import Job, MatchScore, Resume  # noqa: E402
from app.services.collect import cleanup_idle_jobs  # noqa: E402
from app.workers.celery_app import (  # noqa: E402
    idle_jobs_cleanup_task,
    resume_parse_task,
    run_match_task,
)

RESUME_ZH = """张三的简历

工作经历：
2018-2026 某公司 后端工程师，8年开发经验
技术栈：Python、FastAPI、MySQL、K8s

求职意向：高级后端工程师
期望城市：杭州、上海
"""


@pytest.fixture(autouse=True)
def _no_llm_key(monkeypatch):
    """密封 LLM：本机 .env 可能配了 key，任务测试一律走规则路径。"""
    from app.core import config

    monkeypatch.setattr(config.settings, "llm_api_key", "")
    monkeypatch.setattr(config.settings, "resume_parse_async", False)
    monkeypatch.setattr(config.settings, "match_async", False)


@pytest.fixture()
def bind_session(monkeypatch, session):
    """把任务里的 SessionLocal 指向内存库（任务内部是运行时导入，patch 生效）。"""
    monkeypatch.setattr("app.core.db.SessionLocal", lambda: session)
    return session


@pytest.fixture()
def client(session):
    def override_get_session():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_session] = override_get_session
    from fastapi.testclient import TestClient

    yield TestClient(app)
    app.dependency_overrides.pop(get_session, None)


def make_job(external_id="J1", title="Python 后端工程师") -> Job:
    return Job(
        external_id=external_id,
        title=title,
        city="杭州市",
        skills=["python", "fastapi"],
        apply_url=f"https://x.com/{external_id}",
        source="test",
        status="active",
    )


def age_job(session, job: Job, days: int) -> None:
    """把 updated_at 拨回 N 天前（走 table update 绕开 onupdate）。"""
    session.execute(
        Job.__table__.update()
        .where(Job.id == job.id)
        .values(updated_at=datetime.utcnow() - timedelta(days=days))
    )
    session.commit()


# ---------- idle_jobs_cleanup（§9） ----------


def test_cleanup_idle_jobs_expires_stale_keeps_fresh(session):
    stale = make_job("stale")
    fresh = make_job("fresh")
    session.add_all([stale, fresh])
    session.commit()
    age_job(session, stale, days=20)

    result = cleanup_idle_jobs(session, ttl_days=14, rematch=False)
    assert result == {"expired": 1, "ttl_days": 14}
    assert session.get(Job, stale.id).status == "expired"
    assert session.get(Job, fresh.id).status == "active"

    # 幂等：只对 active 生效，第二次为 0
    assert cleanup_idle_jobs(session, ttl_days=14, rematch=False)["expired"] == 0


def test_cleanup_idle_jobs_triggers_rematch(session):
    """下架后重算 match_scores：只剩 active 职位的分数（下架职位不再占位）。"""
    resume = Resume(user_id=1, profile={"skills": ["python"], "target_role": "后端工程师"})
    stale = make_job("stale")
    fresh = make_job("fresh")
    session.add_all([resume, stale, fresh])
    session.commit()
    age_job(session, stale, days=30)

    result = cleanup_idle_jobs(session, ttl_days=14)
    assert result["expired"] == 1
    assert result["rematched"] == 1  # 1 份简历 × 1 个仍 active 的职位
    rows = session.query(MatchScore).all()
    assert [r.job_id for r in rows] == [fresh.id]


def test_idle_jobs_cleanup_task_runs(bind_session):
    job = make_job("t1")
    bind_session.add(job)
    bind_session.commit()
    age_job(bind_session, job, days=20)

    result = idle_jobs_cleanup_task.run(ttl_days=14)
    assert result["expired"] == 1
    assert bind_session.get(Job, job.id).status == "expired"


# ---------- resume_parse_task（§9） ----------


def test_resume_parse_task_parses_and_matches(bind_session):
    job = make_job()
    resume = Resume(user_id=1, raw_text=RESUME_ZH, profile={"parse_status": "pending"})
    bind_session.add_all([job, resume])
    bind_session.commit()

    result = resume_parse_task.run(resume.id)
    assert result["status"] == "ok"
    assert result["source"] == "rules"
    assert result["matched"] == 1

    saved = bind_session.get(Resume, resume.id)
    assert saved.profile["parse_status"] == "done"
    assert saved.profile["experience_years"] == 8
    assert "python" in saved.profile["skills"]
    assert bind_session.query(MatchScore).count() == 1


def test_resume_parse_task_idempotent_when_done(bind_session, monkeypatch):
    """acks_late 重投递：已解析完成的简历直接跳过，不重复调 LLM/覆盖人工修正。"""
    from app.pipelines import parse as parse_mod

    def boom(*_args, **_kwargs):
        raise AssertionError("已完成的简历不应重新解析")

    monkeypatch.setattr(parse_mod, "parse_resume_text", boom)
    resume = Resume(user_id=1, raw_text=RESUME_ZH, profile={"parse_status": "done", "skills": ["go"]})
    bind_session.add(resume)
    bind_session.commit()

    result = resume_parse_task.run(resume.id)
    assert result["status"] == "skipped"
    assert bind_session.get(Resume, resume.id).profile["skills"] == ["go"]


def test_resume_parse_task_missing_resume_returns_error(bind_session):
    assert resume_parse_task.run(999999)["error"] == "resume not found"


def test_resume_parse_task_marks_failed_on_empty_text(bind_session):
    resume = Resume(user_id=1, raw_text="   \n ", profile={"parse_status": "pending"})
    bind_session.add(resume)
    bind_session.commit()

    result = resume_parse_task.run(resume.id)
    assert result["status"] == "failed"
    saved = bind_session.get(Resume, resume.id)
    assert saved.profile["parse_status"] == "failed"
    assert "ValueError" in saved.profile["parse_error"]
    assert bind_session.query(MatchScore).count() == 0


# ---------- run_match_task（§9） ----------


def test_run_match_task_rebuilds_scores(bind_session):
    resume = Resume(user_id=1, profile={"skills": ["python"], "target_role": "后端工程师"})
    bind_session.add_all([resume, make_job("m1"), make_job("m2", "Java 后端工程师")])
    bind_session.commit()

    assert run_match_task.run(resume.id)["matched"] == 2
    assert run_match_task.run(resume.id)["matched"] == 2  # 先删后插，幂等不翻倍
    assert bind_session.query(MatchScore).count() == 2


def test_run_match_task_missing_resume_returns_error(bind_session):
    assert run_match_task.run(999999)["error"] == "resume not found"


# ---------- API 侧投递与降级 ----------


@pytest.fixture()
def enqueued(monkeypatch):
    """替换投递函数：记录 (task_name, args) 并返回预设结果（默认投递成功）。"""
    calls: list[tuple[str, tuple]] = []
    ok = {"value": True}

    def fake(task_name, *args):
        calls.append((task_name, args))
        return ok["value"]

    monkeypatch.setattr("app.main._enqueue", fake)
    return calls, ok


def test_upload_async_queues_parse(client, session, monkeypatch, enqueued):
    from app.core import config

    calls, _ = enqueued
    monkeypatch.setattr(config.settings, "resume_parse_async", True)

    resp = client.post("/api/resumes", data={"raw_text": RESUME_ZH})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["parse_status"] == "pending"
    assert data["profile"] == {"parse_status": "pending"}
    assert calls == [("app.workers.celery_app.resume_parse_task", (data["id"],))]

    # 未解析：不产 match_scores，推荐接口也不做空画像重算
    assert session.query(MatchScore).count() == 0
    assert client.get(f"/api/recommend?resume_id={data['id']}").json()["data"]["total"] == 0

    # 任务跑完 → profile 落库并生成匹配分
    monkeypatch.setattr("app.core.db.SessionLocal", lambda: session)
    assert resume_parse_task.run(data["id"])["status"] == "ok"
    got = client.get(f"/api/resumes/{data['id']}").json()["data"]["profile"]
    assert got["parse_status"] == "done" and got["experience_years"] == 8


def test_upload_async_falls_back_to_sync_when_dispatch_fails(client, monkeypatch, enqueued):
    """开了开关但 broker/worker 不可用 → 请求内同步解析，不静默丢任务。"""
    from app.core import config

    _, ok = enqueued
    ok["value"] = False
    monkeypatch.setattr(config.settings, "resume_parse_async", True)

    data = client.post("/api/resumes", data={"raw_text": RESUME_ZH}).json()["data"]
    assert data["parse_status"] == "done"
    assert data["source"] == "rules"
    assert data["profile"]["experience_years"] == 8


def test_profile_update_async_match(client, session, monkeypatch, enqueued):
    from app.core import config

    calls, _ = enqueued
    monkeypatch.setattr(config.settings, "match_async", True)
    rid = client.post("/api/resumes", data={"raw_text": RESUME_ZH}).json()["data"]["id"]

    resp = client.put(f"/api/resumes/{rid}/profile", json={"profile": {"cities": ["上海"]}})
    data = resp.json()["data"]
    assert data["async"] is True
    assert data["matches_refreshed"] == 0
    assert calls == [("app.workers.celery_app.run_match_task", (rid,))]
    assert session.query(MatchScore).count() == 0  # 重算已交给 worker


def test_profile_update_sync_when_dispatch_fails(client, session, monkeypatch, enqueued):
    from app.core import config

    _, ok = enqueued
    ok["value"] = False
    monkeypatch.setattr(config.settings, "match_async", True)

    job = make_job()
    session.add(job)
    session.commit()
    rid = client.post("/api/resumes", data={"raw_text": RESUME_ZH}).json()["data"]["id"]

    data = client.put(f"/api/resumes/{rid}/profile", json={"profile": {"cities": ["上海"]}}).json()["data"]
    assert "async" not in data
    assert data["matches_refreshed"] == 1  # 降级同步重算