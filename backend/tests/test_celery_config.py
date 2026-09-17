"""Celery beat 调度测试：只验配置与任务注册，不连 broker、不触网。"""

from __future__ import annotations

import pytest

celery = pytest.importorskip("celery")

from app.workers.celery_app import celery_app, collect_company_task, collect_jobs_task


def test_beat_schedule_registered():
    assert "collect-jobs-every-30min" in celery_app.conf.beat_schedule
    entry = celery_app.conf.beat_schedule["collect-jobs-every-30min"]
    assert entry["task"] == "app.workers.celery_app.collect_jobs_task"


def test_tasks_registered():
    names = {t for t in celery_app.tasks if not t.startswith("celery.")}
    assert "app.workers.celery_app.collect_jobs_task" in names
    assert "app.workers.celery_app.collect_company_task" in names


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
