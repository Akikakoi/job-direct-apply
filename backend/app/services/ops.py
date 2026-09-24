"""运维监控看板（§14 ④）：采集成功率 / 任务积压 / 命中率基线 + 告警。

只读聚合，全部复用既有数据与口径，不新增表、不发外呼：
- 采集成功率：`fetch_log` 窗口内按 status 统计。分子 = success，分母 = **全部真实
  尝试**（success + failed + rate_limited + robots_blocked）。限流/被拒是"这轮没采到"
  的事实，不能从分母里剔掉，否则成功率虚高、告警失灵；
- 任务积压：Redis broker 队列长度（`llen`），Redis 不可用 → `available: False`
  并给 `queue: None`（不拿 0 冒充健康）；另给**不依赖 Redis** 的业务口径——
  pending 状态投递数与其中超期未更新数（SQLite 开发环境同样可看）；
- 命中率基线：复用 `insights.build_quality_report` 的 hit_rate / NDCG@k，按样本量与
  下限判定 ok / warn / insufficient_sample；
- alerts：把上述判定收敛成一个列表，运维照单处理（不静默、不美化）。

阈值是可复核的保守值，非拍脑袋；调阈值改本文件常量即可（不改路由/前端）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import redis_utils
from app.core.config import settings
from app.models import Application, FetchLog
from app.services.applications import PENDING_STATUSES
from app.services.insights import build_quality_report

# 采集审计的全部可能状态（与 models.FetchLog.status 口径一致）
FETCH_STATUSES = ("success", "failed", "rate_limited", "robots_blocked")

# Celery 默认队列名（BROKER_URL 未指定 queue 时 beat/worker 共用该队列）
QUEUE_NAME = "celery"

DEFAULT_WINDOW_HOURS = 24

# 阈值：低于/高于即告警
MIN_FETCH_SUCCESS_RATE = 0.8  # 采集成功率下限
MAX_QUEUE_BACKLOG = 100  # 队列积压上限
MIN_HIT_RATE = 0.05  # 命中率基线下限（有反馈样本足够时）
MIN_QUALITY_SAMPLE = 10  # 命中率判定所需的最小有反馈样本量


def collection_health(session: Session, hours: int = DEFAULT_WINDOW_HOURS, now: datetime | None = None) -> dict:
    """窗口内采集成功率：分母含限流/被拒（口径见模块 docstring）。"""
    now = now or datetime.utcnow()
    since = now - timedelta(hours=hours)
    rows = session.execute(
        select(FetchLog.status, func.count())
        .where(FetchLog.started_at >= since)
        .group_by(FetchLog.status)
    ).all()

    by_status: dict[str, int] = {s: 0 for s in FETCH_STATUSES}
    for status, count in rows:
        by_status[status] = by_status.get(status, 0) + int(count)

    attempts = sum(by_status.values())
    success = by_status.get("success", 0)
    rate = round(success / attempts, 4) if attempts else None
    if not attempts:
        status = "insufficient_sample"
    elif rate < MIN_FETCH_SUCCESS_RATE:
        status = "warn"
    else:
        status = "ok"
    return {
        "window_hours": hours,
        "attempts": attempts,
        "success": success,
        "by_status": by_status,
        "success_rate": rate,
        "min_success_rate": MIN_FETCH_SUCCESS_RATE,
        "status": status,
    }


def queue_backlog(client=None) -> dict:
    """Celery 队列积压：Redis 不可用一律 `available: False`（不用 0 冒充健康）。"""
    if client is None:
        client = redis_utils.get_redis()
    if client is None:
        return {"available": False, "queue_name": QUEUE_NAME, "queue": None}
    try:
        return {"available": True, "queue_name": QUEUE_NAME, "queue": int(client.llen(QUEUE_NAME))}
    except Exception:  # 队列读失败等同不可用，不拖垮看板
        return {"available": False, "queue_name": QUEUE_NAME, "queue": None}


def application_backlog(session: Session, now: datetime | None = None) -> dict:
    """业务积压：pending 投递数 + 其中卡超过 T 天未更新的数（与催进扫描同口径）。"""
    now = now or datetime.utcnow()
    threshold = now - timedelta(days=settings.reminder_after_days)

    pending = session.execute(
        select(func.count()).select_from(Application).where(Application.status.in_(PENDING_STATUSES))
    ).scalar() or 0
    stale = session.execute(
        select(func.count())
        .select_from(Application)
        .where(Application.status.in_(PENDING_STATUSES), Application.updated_at <= threshold)
    ).scalar() or 0
    return {
        "pending": int(pending),
        "stale": int(stale),
        "stale_after_days": settings.reminder_after_days,
    }


def quality_baseline(session: Session, k: int = 10) -> dict:
    """命中率基线：复用质量报告，按样本量/下限给状态。"""
    report = build_quality_report(session, k=k)
    responded = sum(report["outcome_counts"].values())
    hit_rate = report["hit_rate"]
    if responded < MIN_QUALITY_SAMPLE:
        status = "insufficient_sample"
    elif hit_rate is not None and hit_rate < MIN_HIT_RATE:
        status = "warn"
    else:
        status = "ok"
    return {
        "hit_rate": hit_rate,
        "ndcg_at_k": report["ndcg_at_k"],
        "feedback_coverage": report["feedback_coverage"],
        "responded": responded,
        "k": k,
        "min_sample": MIN_QUALITY_SAMPLE,
        "min_hit_rate": MIN_HIT_RATE,
        "status": status,
    }


def build_alerts(collection: dict, queue: dict, applications: dict, quality: dict) -> list[dict]:
    """把各项阈值判定收敛成告警列表（level: warn 需处理 / info 仅告知）。"""
    alerts: list[dict] = []

    if collection["status"] == "insufficient_sample":
        alerts.append(
            {
                "level": "info",
                "code": "collection_no_attempt",
                "message": f"最近 {collection['window_hours']} 小时没有采集尝试（beat/worker 是否在跑？）",
            }
        )
    elif collection["status"] == "warn":
        alerts.append(
            {
                "level": "warn",
                "code": "collection_low_success_rate",
                "message": (
                    f"采集成功率 {collection['success_rate']} 低于下限 "
                    f"{collection['min_success_rate']}（窗口 {collection['window_hours']}h，"
                    f"尝试 {collection['attempts']} 次）"
                ),
            }
        )

    if queue["available"] and (queue["queue"] or 0) > MAX_QUEUE_BACKLOG:
        alerts.append(
            {
                "level": "warn",
                "code": "task_queue_backlog",
                "message": f"Celery 队列 {queue['queue_name']} 积压 {queue['queue']} 条，超过上限 {MAX_QUEUE_BACKLOG}",
            }
        )

    if applications["stale"] > 0:
        alerts.append(
            {
                "level": "warn",
                "code": "applications_stale",
                "message": (
                    f"{applications['stale']} 条投递卡超过 {applications['stale_after_days']} 天未更新"
                    f"（pending 共 {applications['pending']} 条）"
                ),
            }
        )

    if quality["status"] == "warn":
        alerts.append(
            {
                "level": "warn",
                "code": "quality_low_hit_rate",
                "message": (
                    f"命中率 {quality['hit_rate']} 低于基线 {quality['min_hit_rate']}"
                    f"（有反馈样本 {quality['responded']}）"
                ),
            }
        )
    elif quality["status"] == "insufficient_sample":
        alerts.append(
            {
                "level": "info",
                "code": "quality_insufficient_sample",
                "message": f"有反馈样本 {quality['responded']} 少于 {quality['min_sample']}，命中率基线暂不判定",
            }
        )

    return alerts


def ops_report(
    session: Session,
    hours: int = DEFAULT_WINDOW_HOURS,
    k: int = 10,
    now: datetime | None = None,
    client=None,
) -> dict:
    """监控看板汇总（接口返回体即本函数返回值）。"""
    collection = collection_health(session, hours=hours, now=now)
    queue = queue_backlog(client=client)
    applications = application_backlog(session, now=now)
    quality = quality_baseline(session, k=k)
    return {
        "window_hours": hours,
        "collection": collection,
        "backlog": {"task_queue": queue, "applications": applications},
        "quality": quality,
        "alerts": build_alerts(collection, queue, applications, quality),
        "notes": [
            "采集成功率的分母含限流/被拒（rate_limited/robots_blocked），不剔除——剔除会让成功率虚高。",
            "队列积压依赖 Redis；Redis 不可用时 available=false，队列口径不看，业务口径（pending/超期）仍可看。",
            "命中率基线口径与 /api/insights/quality 一致：hit=(interview+offer)/有反馈投递。",
        ],
    }