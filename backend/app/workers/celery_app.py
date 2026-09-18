"""Celery 应用 + beat 定时调度（P1 收尾项）。

设计对应开发文档 §2/§5/§9：
- broker/backend：settings.redis_url（默认 redis://localhost:6379/0，
  与 docker-compose.yml 暴露的 Redis 一致）；
- beat 每 30 分钟 tick 一次 collect_jobs_task；collect_all 内部有公司级
  interval_min 间隔守卫，未到期的公司自动 skip，因此高频 tick 安全；
- SQLite/无 Redis 环境下 worker/beat 无法运行，属预期（开发期用
  collect_cli.py 手动触发，生产/联调用 docker-compose 起 Redis）。

启动：
    celery -A app.workers.celery_app worker --loglevel=info
    celery -A app.workers.celery_app beat --loglevel=info
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

BROKER_URL = settings.redis_url or "redis://localhost:6379/0"

celery_app = Celery(
    "jda",
    broker=BROKER_URL,
    backend=BROKER_URL,
    include=["app.workers.celery_app"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Shanghai",
    enable_utc=True,
    # 单 worker 串行采集即可，避免对目标 ATS 站点并发压力
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    beat_schedule={
        # 每 30 分钟 tick；间隔守卫在 collect_all 内部按公司 fetch_policy 生效
        "collect-jobs-every-30min": {
            "task": "app.workers.celery_app.collect_jobs_task",
            "schedule": crontab(minute="*/30"),
        },
        # 每日 09:00 催进扫描（pending 超 T 天），结果打日志；平台内查询式提醒见 /api/reminders
        "reminders-daily": {
            "task": "app.workers.celery_app.scan_reminders_task",
            "schedule": crontab(hour=9, minute=0),
        },
    },
)


@celery_app.task(name="app.workers.celery_app.collect_jobs_task")
def collect_jobs_task() -> dict:
    """采集全部 active 公司（间隔守卫内的公司自动 skip）。"""
    from app.core.db import SessionLocal
    from app.services.collect import collect_all

    with SessionLocal() as session:
        results = collect_all(session)
        return {
            "companies": len(results),
            "ok": sum(1 for r in results if r.get("status") == "ok"),
            "skipped": sum(1 for r in results if r.get("status") == "skipped"),
            "failed": sum(1 for r in results if r.get("status") == "failed"),
            "inserted": sum(int(r.get("inserted", 0)) for r in results),
            "updated": sum(int(r.get("updated", 0)) for r in results),
            "expired": sum(int(r.get("expired", 0)) for r in results),
        }


@celery_app.task(name="app.workers.celery_app.collect_company_task")
def collect_company_task(slug: str) -> dict:
    """按 slug 采集单个公司（供手动补抓 / 单公司调度使用）。"""
    from sqlalchemy import select

    from app.core.db import SessionLocal
    from app.models import Company
    from app.services.collect import collect_company

    with SessionLocal() as session:
        company = session.execute(
            select(Company).where(Company.slug == slug)
        ).scalars().first()
        if company is None:
            return {"error": f"company '{slug}' not found"}
        result = collect_company(session, company)
        return {"slug": slug, **result}


@celery_app.task(name="app.workers.celery_app.scan_reminders_task")
def scan_reminders_task() -> dict:
    """每日催进扫描：pending 卡超 T 天的申请；配了 SMTP 则发汇总邮件，否则打日志。"""
    from app.core.db import SessionLocal
    from app.services.applications import scan_reminders
    from app.services.notify import is_configured, send_reminder_mail

    with SessionLocal() as session:
        items = scan_reminders(session)
        if not items:
            return {"total": 0, "mailed": False}
        if is_configured():
            mailed = send_reminder_mail(items)
        else:
            mailed = False
            for it in items:  # 未配邮件：降级打日志
                print(
                    f"[reminder] application #{it['application_id']} "
                    f"{it['job_title']!r} {it['status']} 卡 {it['stuck_days']} 天 -> {it['apply_url']}"
                )
        return {"total": len(items), "mailed": mailed}
