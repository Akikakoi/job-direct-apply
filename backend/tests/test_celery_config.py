"""Celery beat 调度测试：只验配置与任务注册，不连 broker、不触网。"""

from __future__ import annotations

import pytest

celery = pytest.importorskip("celery")

from app.workers.celery_app import (
    celery_app,
    collect_company_task,
    collect_jobs_task,
    idle_jobs_cleanup_task,
    interview_reminder_task,
    resume_parse_task,
    run_match_task,
    scan_reminders_task,
)


def test_beat_schedule_registered():
    assert "collect-jobs-every-30min" in celery_app.conf.beat_schedule
    entry = celery_app.conf.beat_schedule["collect-jobs-every-30min"]
    assert entry["task"] == "app.workers.celery_app.collect_jobs_task"
    # P3 催进：每日 09:00 扫描
    assert "reminders-daily" in celery_app.conf.beat_schedule
    assert celery_app.conf.beat_schedule["reminders-daily"]["task"] == (
        "app.workers.celery_app.scan_reminders_task"
    )
    # §9 idle_jobs_cleanup：每日 03:00 全局 TTL 下架
    assert celery_app.conf.beat_schedule["idle-jobs-cleanup-daily"]["task"] == (
        "app.workers.celery_app.idle_jobs_cleanup_task"
    )
    # §12.6 P5 ② 面试催进：每日 09:30（与投递催进 09:00 分时）
    assert celery_app.conf.beat_schedule["interview-reminders-daily"]["task"] == (
        "app.workers.celery_app.interview_reminder_task"
    )


def test_tasks_registered():
    names = {t for t in celery_app.tasks if not t.startswith("celery.")}
    for name in (
        "app.workers.celery_app.collect_jobs_task",
        "app.workers.celery_app.collect_company_task",
        "app.workers.celery_app.scan_reminders_task",
        "app.workers.celery_app.idle_jobs_cleanup_task",
        "app.workers.celery_app.resume_parse_task",
        "app.workers.celery_app.run_match_task",
        "app.workers.celery_app.interview_reminder_task",
    ):
        assert name in names
    # §9 三项按 resume_id 入参（API 侧 _enqueue 用任务名 + 位置参数投递）
    assert resume_parse_task.name == "app.workers.celery_app.resume_parse_task"
    assert run_match_task.name == "app.workers.celery_app.run_match_task"
    assert idle_jobs_cleanup_task.name == "app.workers.celery_app.idle_jobs_cleanup_task"


def test_broker_defaults_to_local_redis():
    # 未配置 REDIS_URL 时回退 localhost:6379（与 docker-compose 一致）
    from app.workers import celery_app as mod

    assert mod.BROKER_URL == "redis://localhost:6379/0"


def test_collect_jobs_task_aggregates(session, monkeypatch):
    """任务聚合逻辑：空库（无公司）应返回全零统计，不触网。"""

    monkeypatch.setattr("app.core.db.SessionLocal", lambda: session)
    result = collect_jobs_task.run()
    assert result["companies"] == 0
    assert result["inserted"] == 0
    assert result["expired"] == 0
