"""运维监控看板测试（§14 ④）：采集成功率 / 任务积压 / 命中率基线 + 告警。

全部离线：Redis 由 conftest 的 _seal_redis 密封（get_redis → None），
需要"Redis 可用"的用例自行 monkeypatch 一个假客户端。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.models import Application, FeedbackLog, FetchLog
from app.services.applications import PENDING_STATUSES
from app.services import ops as ops_mod


class FakeQueueRedis:
    """只实现 llen 的最小假客户端（看板只用到这一个命令）。"""

    def __init__(self, length):
        self.length = length
        self.calls: list[str] = []

    def llen(self, name):
        self.calls.append(name)
        if isinstance(self.length, Exception):
            raise self.length
        return self.length


def _log(session, status: str, at: datetime) -> None:
    session.add(FetchLog(company_id=None, status=status, job_count=0, started_at=at))


# ---------- 采集成功率 ----------


def test_collection_health_counts_rate_limited_in_denominator(session):
    """分母含限流/被拒：3 success + 1 failed + 1 rate_limited → 60%（不剔除 = 虚高）。"""
    now = datetime.utcnow()
    for _ in range(3):
        _log(session, "success", now - timedelta(hours=1))
    _log(session, "failed", now - timedelta(hours=1))
    _log(session, "rate_limited", now - timedelta(hours=1))
    session.commit()

    health = ops_mod.collection_health(session, hours=24, now=now)
    assert health["attempts"] == 5
    assert health["by_status"]["robots_blocked"] == 0  # 未出现的状态补 0，结构稳定
    assert health["success_rate"] == 0.6
    assert health["status"] == "warn"


def test_collection_health_excludes_out_of_window_logs(session):
    now = datetime.utcnow()
    _log(session, "failed", now - timedelta(hours=48))
    _log(session, "success", now - timedelta(hours=1))
    session.commit()

    health = ops_mod.collection_health(session, hours=24, now=now)
    assert health["attempts"] == 1  # 48h 前的失败不进窗口
    assert health["success_rate"] == 1.0
    assert health["status"] == "ok"


def test_collection_health_insufficient_sample_without_attempts(session):
    health = ops_mod.collection_health(session, hours=24, now=datetime.utcnow())
    assert health["attempts"] == 0
    assert health["success_rate"] is None
    assert health["status"] == "insufficient_sample"


# ---------- 任务积压 ----------


def test_queue_backlog_unavailable_when_redis_absent():
    """Redis 不可用不得用 0 冒充健康（available=false 且 queue 为 None）。"""
    backlog = ops_mod.queue_backlog()
    assert backlog["available"] is False
    assert backlog["queue"] is None
    assert backlog["queue_name"] == ops_mod.QUEUE_NAME


def test_queue_backlog_reads_llen():
    fake = FakeQueueRedis(150)
    backlog = ops_mod.queue_backlog(client=fake)
    assert backlog == {"available": True, "queue_name": "celery", "queue": 150}
    assert fake.calls == ["celery"]


def test_queue_backlog_degrades_on_redis_error():
    fake = FakeQueueRedis(RuntimeError("connection reset"))
    backlog = ops_mod.queue_backlog(client=fake)
    assert backlog["available"] is False and backlog["queue"] is None


def test_application_backlog_counts_pending_and_stale(session):
    now = datetime.utcnow()
    old = now - timedelta(days=10)
    session.add(Application(user_id=1, status="submitted", updated_at=old))  # 超期
    session.add(Application(user_id=1, status="under_review", updated_at=now))  # 在跟
    session.add(Application(user_id=1, status="offer", updated_at=old))  # 终态不计
    session.commit()

    backlog = ops_mod.application_backlog(session, now=now)
    assert backlog["pending"] == 2
    assert backlog["stale"] == 1
    assert backlog["stale_after_days"] == 3
    assert set(PENDING_STATUSES) == {"submitted", "under_review"}


# ---------- 命中率基线 ----------


def test_quality_baseline_insufficient_sample(session):
    quality = ops_mod.quality_baseline(session, k=10)
    assert quality["responded"] == 0
    assert quality["hit_rate"] is None
    assert quality["status"] == "insufficient_sample"


def test_quality_baseline_warns_below_min_hit_rate(session):
    """12 条全 rejected → 命中率 0 且样本足够 → warn（不因样本少而放过）。"""
    for _ in range(12):
        app = Application(user_id=1, status="rejected")
        session.add(app)
        session.flush()
        session.add(FeedbackLog(application_id=app.id, outcome="rejected"))
    session.commit()

    quality = ops_mod.quality_baseline(session, k=10)
    assert quality["responded"] == 12
    assert quality["hit_rate"] == 0.0
    assert quality["status"] == "warn"


def test_quality_baseline_ok_with_enough_hits(session):
    """样本足够且命中率不低于下限 → ok。"""
    for i in range(10):
        app = Application(user_id=1, status="interview")
        session.add(app)
        session.flush()
        session.add(FeedbackLog(application_id=app.id, outcome="offer" if i < 3 else "rejected"))
    session.commit()

    quality = ops_mod.quality_baseline(session, k=10)
    assert quality["responded"] == 10
    assert quality["hit_rate"] == 0.3
    assert quality["status"] == "ok"


# ---------- 汇总与告警 ----------


def test_ops_report_shape_and_alerts(session):
    now = datetime.utcnow()
    _log(session, "failed", now - timedelta(hours=1))  # 采集 0% → warn
    session.add(Application(user_id=1, status="submitted", updated_at=now - timedelta(days=5)))
    session.commit()

    report = ops_mod.ops_report(
        session, hours=24, k=10, now=now, client=FakeQueueRedis(ops_mod.MAX_QUEUE_BACKLOG + 1)
    )

    assert report["window_hours"] == 24
    assert set(report) == {"window_hours", "collection", "backlog", "quality", "alerts", "notes"}
    codes = {a["code"] for a in report["alerts"]}
    assert {
        "collection_low_success_rate",
        "task_queue_backlog",
        "applications_stale",
        "quality_insufficient_sample",
    } <= codes
    assert all(a["level"] in ("warn", "info") for a in report["alerts"])
    assert report["backlog"]["task_queue"]["queue"] == ops_mod.MAX_QUEUE_BACKLOG + 1
    assert len(report["notes"]) == 3


def test_ops_report_silent_when_all_healthy(session):
    """全绿时不留 warn（告警不许长鸣，否则没人看）。"""
    now = datetime.utcnow()
    for i in range(10):
        app = Application(user_id=1, status="interview")
        session.add(app)
        session.flush()
        session.add(FeedbackLog(application_id=app.id, outcome="offer" if i < 2 else "rejected"))
    _log(session, "success", now - timedelta(hours=1))
    session.commit()

    report = ops_mod.ops_report(session, hours=24, k=10, now=now, client=FakeQueueRedis(0))
    assert report["alerts"] == []


# ---------- API ----------


@pytest.fixture(autouse=True)
def _seal_queue_redis(monkeypatch):
    """让看板里的队列口径默认走"Redis 不可用"路径（与生产无 Redis 时一致）。"""
    monkeypatch.setattr(ops_mod.redis_utils, "get_redis", lambda: None)


def test_ops_insights_api(client):
    resp = client.get("/api/insights/ops")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["collection"]["status"] == "insufficient_sample"
    assert data["backlog"]["task_queue"]["available"] is False
    assert data["quality"]["min_sample"] == ops_mod.MIN_QUALITY_SAMPLE


def test_ops_insights_api_accepts_window_and_k(client):
    resp = client.get("/api/insights/ops?hours=168&k=5")
    assert resp.status_code == 200
    assert resp.json()["data"]["window_hours"] == 168
    assert resp.json()["data"]["quality"]["k"] == 5


@pytest.mark.parametrize("query", ["hours=0", "hours=100000", "k=0", "k=999"])
def test_ops_insights_api_rejects_out_of_range(client, query):
    assert client.get(f"/api/insights/ops?{query}").status_code == 422


# ---------- 前端契约（前端无自动化测试框架，沿用静态资产断言） ----------

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
INSIGHTS = FRONTEND / "app" / "insights" / "page.js"
I18N = FRONTEND / "app" / "i18n.js"


def test_insights_page_renders_ops_card():
    source = INSIGHTS.read_text(encoding="utf-8")
    assert "/api/insights/ops?hours=${opsHours}" in source
    assert "ops_title" in source
    # 后端状态 insufficient_sample 与词典 key ops_status_insufficient 不对齐会静默漏译 → 断言映射存在
    assert 'insufficient_sample: "ops_status_insufficient"' in source
    assert "ops_level_" in source  # 告警等级标签走同一词典
    assert "ops_queue_na" in source  # Redis 不可用如实显示，不显示 0


def test_ops_i18n_keys_present_in_both_dicts():
    """ops_* 每个 key 在中英词典各出现一次（漏一路会静默显示 key 本身）。"""
    source = I18N.read_text(encoding="utf-8")
    for key in (
        "ops_title",
        "ops_sub",
        "ops_m_success",
        "ops_m_queue",
        "ops_m_hit",
        "ops_over_days",
        "ops_status_ok",
        "ops_status_warn",
        "ops_status_insufficient",
        "ops_alerts",
        "ops_no_alerts",
        "ops_level_warn",
        "ops_level_info",
    ):
        assert source.count(f"\n  {key}: ") == 2, f"{key} 未在中英词典各出现一次"