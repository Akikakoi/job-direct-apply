"""§10 可观测性用例：JSON 日志、request_id、Prometheus 指标与 /metrics 出口。

重点覆盖三件容易"看着有、其实没用"的事：
1. **标签基数**：HTTP 指标的 path 必须是路由模板（`/api/resumes/{resume_id}`），
   否则每条用户数据长一条时间序列，Prometheus 迟早被撑爆；
2. **5xx 也要计数**：异常路径漏记等于把最该看见的请求丢掉；
3. **指标与日志不泄露密钥/个人信息**（抓取口通常是内网裸奔的）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

import pytest

from app.core.config import settings
from app.models import Application, FetchLog
from app.services import obs


@pytest.fixture(autouse=True)
def _clean_metrics():
    obs.reset_metrics()
    yield
    obs.reset_metrics()


def _sample_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line and not line.startswith("#")]


# ---------- JSON 日志 ----------


def test_json_formatter_emits_single_line_json_with_context():
    obs.request_id_var.set("rid-1")
    record = logging.LogRecord(
        name="jda.http", level=logging.INFO, pathname=__file__, lineno=1,
        msg="http_request", args=(), exc_info=None,
    )
    record.event = "http_request"
    record.status = 200
    payload = json.loads(obs.JsonFormatter().format(record))
    obs.request_id_var.set("")

    assert payload["level"] == "INFO"
    assert payload["logger"] == "jda.http"
    assert payload["msg"] == "http_request"
    assert payload["event"] == "http_request"
    assert payload["status"] == 200
    assert payload["request_id"] == "rid-1"
    assert payload["ts"].endswith("Z") and "T" in payload["ts"]
    assert "levelname" not in payload  # 内置名不重复输出
    assert "\n" not in json.dumps(payload)


def test_json_formatter_keeps_only_exception_type_not_stack():
    try:
        raise ValueError("boom zhangsan@example.com")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="jda", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="failed", args=(), exc_info=sys.exc_info(),
        )
    payload = json.loads(obs.JsonFormatter().format(record))
    assert payload["exc_type"] == "ValueError"
    assert "Traceback" not in json.dumps(payload)  # 不落堆栈正文


def test_setup_logging_is_idempotent_and_toggleable():
    assert obs.setup_logging(force=True) is True
    assert obs.setup_logging(force=True) is True
    root = logging.getLogger()
    assert len([h for h in root.handlers if h.name == obs._HANDLER_NAME]) == 1
    assert obs.setup_logging(force=False) is False
    assert not [h for h in root.handlers if h.name == obs._HANDLER_NAME]


def test_setup_logging_reads_settings(monkeypatch):
    monkeypatch.setattr(settings, "log_json", True)
    assert obs.setup_logging() is True
    monkeypatch.setattr(settings, "log_json", False)
    assert obs.setup_logging() is False


def test_log_http_noop_unless_json_logging_enabled(monkeypatch, caplog):
    monkeypatch.setattr(settings, "log_json", False)
    with caplog.at_level(logging.INFO, logger="jda.http"):
        obs.log_http("GET", "/health", 200, 1.5)
    assert caplog.records == []

    monkeypatch.setattr(settings, "log_json", True)
    with caplog.at_level(logging.INFO, logger="jda.http"):
        obs.log_http("GET", "/health", 200, 1.5)
    record = caplog.records[-1]
    assert record.event == "http_request"
    assert record.path == "/health" and record.status == 200
    assert record.duration_ms == 1.5


# ---------- request_id 与指标 ----------


def test_request_id_is_echoed_and_generated(client):
    echoed = client.get("/health", headers={obs.REQUEST_ID_HEADER: "abc123"})
    assert echoed.headers[obs.REQUEST_ID_HEADER] == "abc123"

    generated = client.get("/health")
    rid = generated.headers[obs.REQUEST_ID_HEADER]
    assert len(rid) == 16 and rid != "abc123"


def test_http_metrics_count_requests_by_route_template(client):
    client.get("/health")
    client.get("/health")
    assert obs.HTTP_REQUESTS.value(method="GET", path="/health", status="200") == 2

    # 路径参数走模板，不是原始路径（否则 /api/resumes/1、/2… 各长一条序列）
    client.get("/api/resumes/999")
    assert obs.HTTP_REQUESTS.value(method="GET", path="/api/resumes/{resume_id}", status="404") == 1
    assert obs.HTTP_REQUESTS.value(method="GET", path="/api/resumes/999", status="404") == 0

    # 未匹配路由归 unmatched，不让扫描器把基数撑爆
    client.get("/not-a-real-route")
    assert obs.HTTP_REQUESTS.value(method="GET", path="unmatched", status="404") == 1


def test_histogram_records_duration_with_buckets(client):
    client.get("/health")
    text = "\n".join(obs.HTTP_DURATION.render())
    assert "jda_http_request_duration_seconds_bucket" in text
    assert 'le="+Inf"' in text
    assert "jda_http_request_duration_seconds_count" in text
    assert "jda_http_request_duration_seconds_sum" in text
    assert obs.HTTP_DURATION.count(method="GET", path="/health") == 1


def test_prometheus_text_is_wellformed():
    text = obs.prometheus_text(with_ops=False)
    assert text.endswith("\n")
    assert "# HELP jda_http_requests_total" in text and "# TYPE jda_http_requests_total counter" in text
    for line in _sample_lines(text):
        name, _, value = line.rpartition(" ")
        assert name and value
        float(value)  # 值必须是数字
        assert " " not in name  # 名字+标签里不能有裸空格


# ---------- /metrics 出口 ----------


def test_metrics_endpoint_returns_prometheus_text(client):
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain; version=0.0.4")
    body = resp.text
    assert "jda_app_info{version=" in body
    assert "jda_http_requests_total" in body
    assert "jda_collect_attempts_total" in body  # 业务口径随抓取刷新
    # 抓取本身也计数：中间件在响应之后才记，所以本轮响应看不到自己，下一次抓取能看到
    assert 'path="/metrics"' not in body
    assert 'path="/metrics"' in client.get("/metrics").text


def test_metrics_token_gate(client, monkeypatch):
    assert client.get("/metrics").status_code == 200  # 默认不强制

    monkeypatch.setattr(settings, "metrics_token", "s3cr3t-token-abcdefgh")
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics?token=wrong").status_code == 401
    assert client.get("/metrics?token=s3cr3t-token-abcdefgh").status_code == 200
    assert client.get(
        "/metrics", headers={"Authorization": "Bearer s3cr3t-token-abcdefgh"}
    ).status_code == 200


def test_metrics_disabled_returns_404(client, monkeypatch):
    monkeypatch.setattr(settings, "metrics_enabled", False)
    assert client.get("/metrics").status_code == 404


def test_metrics_expose_ops_gauges(client, session):
    now = datetime.utcnow()
    session.add(FetchLog(company_id=None, status="success", started_at=now))
    session.add(FetchLog(company_id=None, status="rate_limited", started_at=now))
    session.add(Application(user_id=1, job_id=None, status="submitted", authorized=True))
    session.commit()

    body = client.get("/metrics").text
    assert 'jda_collect_attempts_total{window_hours="24"} 2' in body  # 分母含限流（口径与 ops 同源）
    assert 'jda_collect_attempts{status="rate_limited"} 1' in body
    assert "jda_applications_pending 1" in body
    assert "jda_celery_queue_available 0" in body  # 测试密封 Redis → 如实报不可用，不拿 0 冒充
    assert "jda_ops_available 1" in body


def test_metrics_and_logs_never_leak_secrets(client, monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "sk-super-secret-value-123456")
    monkeypatch.setattr(settings, "uploads_key", "aGVsbG8td29ybGQtdGhpcy1pcy1rZXk=")
    body = client.get("/metrics").text
    assert "sk-super-secret-value-123456" not in body
    assert "aGVsbG8td29ybGQtdGhpcy1pcy1rZXk=" not in body
    assert "zhangsan" not in body


# ---------- Sentry（可选依赖） ----------


def test_sentry_disabled_without_dsn(monkeypatch):
    monkeypatch.setattr(settings, "sentry_dsn", "")
    assert obs.init_sentry() is False
    assert obs.sentry_status()["reason"] == "no_dsn"


def test_sentry_init_never_raises_and_reports_state(monkeypatch):
    """没装 sentry-sdk 也必须能启动（可观测性是增强项，不能变成启动依赖）。"""
    monkeypatch.setattr(settings, "sentry_dsn", "https://public@example.invalid/1")
    result = obs.init_sentry()
    status = obs.sentry_status()
    assert status["reason"] in {"ok", "sdk_not_installed"}
    assert result is (status["reason"] == "ok")
