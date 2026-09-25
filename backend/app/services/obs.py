"""可观测性（§10）：结构化 JSON 日志 + Prometheus 指标 + Sentry（可选）。

三件事都**零依赖**（Prometheus 文本格式手写、日志用标准库 `logging`），只有 Sentry
需要外部 SDK，且**没装也不报错**——可观测性是增强项，不能变成启动依赖：

1. **结构化日志**：`setup_logging()` 装一个 JSON 格式化器（`LOG_JSON=true` 才开，默认关，
   免得开发期控制台全是 JSON）；每条 HTTP 请求带 `request_id`（回显 `X-Request-ID`，
   没有就生成），出问题时能按 id 串起一条链路。
2. **指标**：进程内计数器/直方图/仪表 + 文本格式导出，`GET /metrics` 暴露。HTTP 指标
   的 `path` 标签用**路由模板**（`/api/resumes/{resume_id}`）而不是原始路径——否则
   `/api/resumes/1`、`/api/resumes/2` 会把时间序列撑爆；未匹配路由一律归 `unmatched`。
   业务口径（采集成功率/队列积压/命中率）**复用 `ops.ops_report`**，不在 scraping 路径上
   另写一套统计（同一件事两套口径 = 迟早对不上）。
3. **Sentry**：配了 `SENTRY_DSN` 且装了 `sentry-sdk` 才初始化；否则静默返回 False
   （并在 `/metrics` 的 `jda_sentry_enabled` 上如实体现）。

安全底线：指标与日志**不回显密钥/简历正文**；`/metrics` 不暴露任何个人信息，只在
`METRICS_TOKEN` 配置时要求令牌（Prometheus 抓取通常无凭据，故默认不强制）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from contextvars import ContextVar
from datetime import datetime

from starlette.routing import Match

from app.core.config import settings

# 应用版本（与 main.FastAPI(version=...) 一致，作为指标标签用，不参与业务判断）
APP_VERSION = "0.1.0"

# ---------- 请求上下文 ----------

REQUEST_ID_HEADER = "X-Request-ID"
request_id_var: ContextVar[str] = ContextVar("jda_request_id", default="")


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def current_request_id() -> str:
    return request_id_var.get()


# ---------- 结构化日志 ----------


class JsonFormatter(logging.Formatter):
    """一行一个 JSON 对象：`{"ts","level","logger","msg",...extra}`。

    额外字段来自 `logger.info("...", extra={"event": ..., "duration_ms": ...})`——
    固定字段与 extra 平铺在同一层，方便 Loki/ES 直接按字段检索。
    """

    # 与 LogRecord 内置属性重名的键不重复输出（否则每条日志都拖一堆噪音）
    _RESERVED = frozenset(
        {
            "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
            "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
            "relativeCreated", "thread", "threadName", "processName", "process", "taskName",
            "message", "asctime",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.utcfromtimestamp(record.created).isoformat(timespec="milliseconds") + "Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = current_request_id()
        if rid:
            payload["request_id"] = rid
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            # 只留异常类型与消息，不留堆栈正文（堆栈里可能带用户数据）
            payload["exc_type"] = getattr(record.exc_info[0], "__name__", "Exception")
        return json.dumps(payload, ensure_ascii=False, default=str)


_HANDLER_NAME = "jda-json"


def setup_logging(force: bool | None = None) -> bool:
    """按 `settings.log_json` 装/卸 JSON 处理器；返回最终是否启用。

    幂等：重复调用只保留一个 handler，不叠加。默认关（`force=None` 时读配置）。
    """
    enabled = bool(settings.log_json) if force is None else bool(force)
    root = logging.getLogger()
    for handler in list(root.handlers):
        if handler.name == _HANDLER_NAME:
            root.removeHandler(handler)
    if not enabled:
        return False
    handler = logging.StreamHandler()
    handler.name = _HANDLER_NAME
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    return True


http_logger = logging.getLogger("jda.http")


def log_http(method: str, path: str, status: int, duration_ms: float) -> None:
    """打一条请求日志（仅在开启 JSON 日志时输出，避免开发期控制台被刷屏）。"""
    if not settings.log_json:
        return
    http_logger.info(
        "http_request",
        extra={
            "event": "http_request",
            "method": method,
            "path": path,
            "status": status,
            "duration_ms": round(duration_ms, 2),
        },
    )


# ---------- 指标 ----------


def _escape_label(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(pairs: dict | None) -> str:
    if not pairs:
        return ""
    inner = ",".join(f'{k}="{_escape_label(v)}"' for k, v in sorted(pairs.items()))
    return "{" + inner + "}"


class Counter:
    """单调递增计数器（按标签组合分桶）。"""

    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...] = ()):
        self.name = name
        self.help = help_text
        self.label_names = label_names
        self._values: dict[tuple, float] = {}
        self._lock = threading.Lock()

    def inc(self, amount: float = 1, **labels) -> None:
        key = tuple(labels.get(name, "") for name in self.label_names)
        with self._lock:
            self._values[key] = self._values.get(key, 0) + amount

    def value(self, **labels) -> float:
        key = tuple(labels.get(name, "") for name in self.label_names)
        with self._lock:
            return self._values.get(key, 0)

    def reset(self) -> None:
        with self._lock:
            self._values.clear()

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        with self._lock:
            items = sorted(self._values.items())
        for key, value in items:
            pairs = dict(zip(self.label_names, key)) if self.label_names else None
            lines.append(f"{self.name}{_labels(pairs)} {_format_number(value)}")
        return lines


class Gauge:
    """可增可减的仪表（同一标签组合覆盖写）。"""

    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...] = ()):
        self.name = name
        self.help = help_text
        self.label_names = label_names
        self._values: dict[tuple, float] = {}
        self._lock = threading.Lock()

    def set(self, value: float, **labels) -> None:
        key = tuple(labels.get(name, "") for name in self.label_names)
        with self._lock:
            self._values[key] = float(value)

    def value(self, **labels) -> float | None:
        key = tuple(labels.get(name, "") for name in self.label_names)
        with self._lock:
            return self._values.get(key)

    def reset(self) -> None:
        with self._lock:
            self._values.clear()

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} gauge"]
        with self._lock:
            items = sorted(self._values.items())
        for key, value in items:
            pairs = dict(zip(self.label_names, key)) if self.label_names else None
            lines.append(f"{self.name}{_labels(pairs)} {_format_number(value)}")
        return lines


class Histogram:
    """固定分桶直方图（只暴露 `_bucket`/`_sum`/`_count`，够算 P50/P95/P99）。"""

    DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...] = (), buckets=None):
        self.name = name
        self.help = help_text
        self.label_names = label_names
        self.buckets = tuple(buckets or self.DEFAULT_BUCKETS)
        self._counts: dict[tuple, list[int]] = {}
        self._sums: dict[tuple, float] = {}
        self._totals: dict[tuple, int] = {}
        self._lock = threading.Lock()

    def observe(self, value: float, **labels) -> None:
        key = tuple(labels.get(name, "") for name in self.label_names)
        with self._lock:
            counts = self._counts.setdefault(key, [0] * len(self.buckets))
            for i, edge in enumerate(self.buckets):
                if value <= edge:
                    counts[i] += 1
            self._sums[key] = self._sums.get(key, 0.0) + float(value)
            self._totals[key] = self._totals.get(key, 0) + 1

    def count(self, **labels) -> int:
        key = tuple(labels.get(name, "") for name in self.label_names)
        with self._lock:
            return self._totals.get(key, 0)

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()
            self._sums.clear()
            self._totals.clear()

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} histogram"]
        with self._lock:
            items = sorted(self._counts.items())
            sums = dict(self._sums)
            totals = dict(self._totals)
        for key, counts in items:
            pairs = dict(zip(self.label_names, key)) if self.label_names else {}
            for edge, count in zip(self.buckets, counts):
                lines.append(
                    f"{self.name}_bucket{_labels({**pairs, 'le': _format_number(edge)})} {count}"
                )
            lines.append(f"{self.name}_bucket{_labels({**pairs, 'le': '+Inf'})} {totals.get(key, 0)}")
            lines.append(f"{self.name}_sum{_labels(pairs or None)} {_format_number(sums.get(key, 0.0))}")
            lines.append(f"{self.name}_count{_labels(pairs or None)} {totals.get(key, 0)}")
        return lines


def _format_number(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return repr(round(float(value), 6))


# 进程级指标（模块单例：一个进程一份，够 Prometheus 抓取；多进程部署靠 Prometheus 自带聚合）
HTTP_REQUESTS = Counter(
    "jda_http_requests_total", "HTTP 请求数（path 用路由模板，未匹配归 unmatched）", ("method", "path", "status")
)
HTTP_DURATION = Histogram(
    "jda_http_request_duration_seconds", "HTTP 请求耗时（秒）", ("method", "path")
)
APP_INFO = Gauge("jda_app_info", "应用信息（恒为 1，版本在标签上）", ("version",))
SENTRY_ENABLED = Gauge("jda_sentry_enabled", "Sentry 是否已初始化（1/0）")
OPS_AVAILABLE = Gauge("jda_ops_available", "抓取时业务口径是否可用（1/0，DB/聚合失败为 0）")

# 业务口径（抓取时从 ops.ops_report 取值，不在中间件里另算）
COLLECT_SUCCESS_RATIO = Gauge("jda_collect_success_ratio", "窗口内采集成功率（分母含限流/被拒）", ("window_hours",))
COLLECT_ATTEMPTS = Gauge("jda_collect_attempts_total", "窗口内采集尝试次数", ("window_hours",))
COLLECT_BY_STATUS = Gauge("jda_collect_attempts", "窗口内采集尝试（按状态）", ("status",))
QUEUE_AVAILABLE = Gauge("jda_celery_queue_available", "Celery 队列口径是否可用（Redis 不可用为 0）")
QUEUE_LENGTH = Gauge("jda_celery_queue_length", "Celery 队列积压条数", ("queue",))
APPLICATIONS_PENDING = Gauge("jda_applications_pending", "进行中的投递数")
APPLICATIONS_STALE = Gauge("jda_applications_stale", "卡超过阈值的投递数")
FEEDBACK_HIT_RATE = Gauge("jda_feedback_hit_rate", "命中率基线（interview+offer / 有反馈投递）")
FEEDBACK_RESPONDED = Gauge("jda_feedback_responded", "有反馈的投递数（样本量）")
NDCG_AT_K = Gauge("jda_ndcg_at_k", "推荐序与反馈相关度的 NDCG@k", ("k",))
OPS_ALERTS = Gauge("jda_ops_alerts", "运维告警条数（按级别）", ("level",))


def record_request(method: str, path: str, status: int, duration_s: float) -> None:
    """记录一次请求（中间件唯一入口）。"""
    HTTP_REQUESTS.inc(method=method, path=path, status=str(status))
    HTTP_DURATION.observe(duration_s, method=method, path=path)


def collect_ops_gauges(session=None, hours: int | None = None, k: int = 10) -> bool:
    """抓取时刷新业务口径指标；失败不抛异常（指标抓不到不能拖垮 /metrics）。"""
    from app.core.db import SessionLocal
    from app.services import ops

    own_session = session is None
    if own_session:
        session = SessionLocal()
    try:
        report = ops.ops_report(session, hours=hours or settings.metrics_ops_window_hours, k=k)
    except Exception:
        OPS_AVAILABLE.set(0)
        return False
    finally:
        if own_session:
            session.close()

    window = str(report["window_hours"])
    collection = report["collection"]
    COLLECT_SUCCESS_RATIO.set(collection["success_rate"] or 0, window_hours=window)
    COLLECT_ATTEMPTS.set(collection["attempts"], window_hours=window)
    for status_name, count in collection["by_status"].items():
        COLLECT_BY_STATUS.set(count, status=status_name)

    queue = report["backlog"]["task_queue"]
    QUEUE_AVAILABLE.set(1 if queue["available"] else 0)
    if queue["available"] and queue["queue"] is not None:
        QUEUE_LENGTH.set(queue["queue"], queue=queue["queue_name"])

    applications = report["backlog"]["applications"]
    APPLICATIONS_PENDING.set(applications["pending"])
    APPLICATIONS_STALE.set(applications["stale"])

    quality = report["quality"]
    FEEDBACK_HIT_RATE.set(quality["hit_rate"] or 0)
    FEEDBACK_RESPONDED.set(quality["responded"])
    NDCG_AT_K.set(quality["ndcg_at_k"] or 0, k=str(quality["k"]))

    levels = {"warn": 0, "info": 0}
    for alert in report["alerts"]:
        levels[alert["level"]] = levels.get(alert["level"], 0) + 1
    for level, count in levels.items():
        OPS_ALERTS.set(count, level=level)

    OPS_AVAILABLE.set(1)
    return True


def prometheus_text(with_ops: bool = True, session=None) -> str:
    """渲染 Prometheus 文本格式（含 HELP/TYPE 头）。"""
    APP_INFO.set(1, version=APP_VERSION)
    SENTRY_ENABLED.set(1 if _SENTRY_STATE["enabled"] else 0)
    if with_ops:
        collect_ops_gauges(session=session)
    lines: list[str] = []
    for metric in (
        APP_INFO,
        SENTRY_ENABLED,
        OPS_AVAILABLE,
        HTTP_REQUESTS,
        HTTP_DURATION,
        COLLECT_SUCCESS_RATIO,
        COLLECT_ATTEMPTS,
        COLLECT_BY_STATUS,
        QUEUE_AVAILABLE,
        QUEUE_LENGTH,
        APPLICATIONS_PENDING,
        APPLICATIONS_STALE,
        FEEDBACK_HIT_RATE,
        FEEDBACK_RESPONDED,
        NDCG_AT_K,
        OPS_ALERTS,
    ):
        lines.extend(metric.render())
    return "\n".join(lines) + "\n"


def reset_metrics() -> None:
    """清空进程内指标（测试用；生产不在运行时调用——指标是累积量，清空等于丢失历史）。"""
    for metric in (
        HTTP_REQUESTS, HTTP_DURATION, COLLECT_SUCCESS_RATIO, COLLECT_ATTEMPTS, COLLECT_BY_STATUS,
        QUEUE_AVAILABLE, QUEUE_LENGTH, APPLICATIONS_PENDING, APPLICATIONS_STALE, FEEDBACK_HIT_RATE,
        FEEDBACK_RESPONDED, NDCG_AT_K, OPS_ALERTS,
    ):
        metric.reset()


# ---------- Sentry（可选） ----------

_SENTRY_STATE: dict = {"enabled": False, "reason": "not_initialized"}


def init_sentry(dsn: str | None = None) -> bool:
    """配了 DSN 且装了 sentry-sdk 才初始化；否则静默返回 False（不阻塞启动）。"""
    dsn = dsn if dsn is not None else settings.sentry_dsn
    if not dsn:
        _SENTRY_STATE.update(enabled=False, reason="no_dsn")
        return False
    try:
        import sentry_sdk  # 可选依赖：pip install sentry-sdk
    except ImportError:
        _SENTRY_STATE.update(enabled=False, reason="sdk_not_installed")
        return False
    sentry_sdk.init(dsn=dsn, environment=settings.sentry_environment, traces_sample_rate=0.0)
    _SENTRY_STATE.update(enabled=True, reason="ok")
    return True


def sentry_status() -> dict:
    return dict(_SENTRY_STATE)


# ---------- HTTP 中间件 ----------


def route_template(request) -> str:
    """路由模板（如 `/api/resumes/{resume_id}`）；未匹配路由归 `unmatched`。

    用模板而不是原始路径是关键：`/api/resumes/1`、`/2`、`/3`… 会各成一条时间序列，
    指标基数随用户数据无限膨胀，Prometheus 迟早被撑爆。

    实现说明：starlette 1.6 起匹配结果**不再写进 `scope["route"]`**（只给 endpoint/
    path_params），所以这里按官方 `route.matches(scope)` 自己找——FULL 优先，
    次选 PARTIAL（405 这类"路径对上、方法不对"也算在该路由名下，不归 unmatched）。
    """
    scope = request.scope
    routes = getattr(getattr(request, "app", None), "router", None)
    partial = None
    for candidate in getattr(routes, "routes", None) or []:
        matches = getattr(candidate, "matches", None)
        if matches is None:
            continue
        try:
            match, _ = matches(scope)
        except Exception:  # 个别路由的 matches 对异常 scope 会抛错，跳过即可
            continue
        if match == Match.FULL:
            return _template_of(candidate)
        if match == Match.PARTIAL and partial is None:
            partial = candidate
    return _template_of(partial) if partial is not None else "unmatched"


def _template_of(route) -> str:
    path = getattr(route, "path", None)
    return str(path) if path else "unmatched"


async def http_observability(request, call_next):
    """记录请求指标 + 回显/生成 `X-Request-ID`（并写入日志上下文）。

    用 `finally` 记录：handler 抛异常时也计入 5xx（否则"错误请求"在指标里完全消失，
    正好漏掉最该被看到的那一类）。
    """
    rid = request.headers.get(REQUEST_ID_HEADER) or new_request_id()
    token = request_id_var.set(rid)
    method = request.method
    path = route_template(request)
    started = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers[REQUEST_ID_HEADER] = rid
        return response
    finally:
        duration = time.perf_counter() - started
        request_id_var.reset(token)
        record_request(method, path, status_code, duration)
        log_http(method, path, status_code, duration * 1000)
