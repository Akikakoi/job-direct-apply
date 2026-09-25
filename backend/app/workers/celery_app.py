"""Celery 应用 + beat 定时调度（P1 收尾项）。

设计对应开发文档 §2/§5/§9：
- broker/backend：settings.redis_url（默认 redis://localhost:6379/0，
  与 docker-compose.yml 暴露的 Redis 一致）；
- beat 每 30 分钟 tick 一次 collect_jobs_task；collect_all 内部有公司级
  interval_min 间隔守卫，未到期的公司自动 skip，因此高频 tick 安全；
- §9 其余三项：idle_jobs_cleanup_task（每日 03:00 全局 TTL 下架）、
  resume_parse_task / run_match_task（按 resume_id 异步解析与重算，由 API 侧
  settings.resume_parse_async / match_async 开关投递，投递失败自动降级为请求内同步）；
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
        # 每日 03:00 全局 TTL 下架：兜底停用/删除公司留下的孤儿职位（§9 idle_jobs_cleanup）
        "idle-jobs-cleanup-daily": {
            "task": "app.workers.celery_app.idle_jobs_cleanup_task",
            "schedule": crontab(hour=3, minute=0),
        },
        # 每日 09:30 面试催进（§12.6 P5 ②）：面试时间临近/刚过未更新结果的申请
        "interview-reminders-daily": {
            "task": "app.workers.celery_app.interview_reminder_task",
            "schedule": crontab(hour=9, minute=30),
        },
    },
)

# §12.5 反馈回灌闭环：周度自动采纳（默认关——自动改线上排序不能默认开；
# 开 = WEIGHTS_AUTO_TUNE=true，且采纳仍受样本/增益双门槛约束）
if settings.weights_auto_tune:
    celery_app.conf.beat_schedule["weights-auto-tune-weekly"] = {
        "task": "app.workers.celery_app.auto_tune_weights_task",
        "schedule": crontab(day_of_week="mon", hour=6, minute=0),
    }


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
    """每日催进扫描：配了 SMTP 发邮件、配了 IM webhook 推群，均未配置则打日志。"""
    from app.core.db import SessionLocal
    from app.services.applications import scan_reminders
    from app.services.notify import is_configured, is_im_configured, send_reminder_im, send_reminder_mail

    with SessionLocal() as session:
        items = scan_reminders(session)
        if not items:
            return {"total": 0, "mailed": False, "im_sent": False}
        mailed = send_reminder_mail(items) if is_configured() else False
        im_sent = send_reminder_im(items) if is_im_configured() else False
        if not mailed and not im_sent:
            for it in items:  # 邮件/IM 均未配置：降级打日志
                print(
                    f"[reminder] application #{it['application_id']} "
                    f"{it['job_title']!r} {it['status']} 卡 {it['stuck_days']} 天 -> {it['apply_url']}"
                )
        return {"total": len(items), "mailed": mailed, "im_sent": im_sent}


@celery_app.task(name="app.workers.celery_app.idle_jobs_cleanup_task")
def idle_jobs_cleanup_task(ttl_days: int | None = None) -> dict:
    """每日全局 TTL 下架（§9）：active 且超 TTL 天未保鲜 → expired，并重算匹配分。"""
    from app.core.db import SessionLocal
    from app.services.collect import cleanup_idle_jobs

    with SessionLocal() as session:
        return cleanup_idle_jobs(session, ttl_days=ttl_days)


@celery_app.task(name="app.workers.celery_app.resume_parse_task")
def resume_parse_task(resume_id: int) -> dict:
    """上传后异步解析（§9）：按 resume_id 重新解析并落 profile，再重算该简历匹配分。

    幂等：profile.parse_status == "done" 时直接返回（acks_late 下的重投递不重复调 LLM）。
    失败不抛异常，把 parse_status 置 failed 并记 notes，便于前端展示与人工重试。
    """
    from app.core.db import SessionLocal
    from app.models import Resume
    from app.pipelines.match import refresh_matches
    from app.pipelines.parse import parse_resume_text

    with SessionLocal() as session:
        resume = session.get(Resume, resume_id)
        if resume is None:
            return {"resume_id": resume_id, "error": "resume not found"}
        profile = dict(resume.profile or {})
        if profile.get("parse_status") == "done":
            return {"resume_id": resume_id, "status": "skipped", "reason": "already_parsed"}
        try:
            parsed = parse_resume_text(resume.raw_text or "", session)
        except Exception as exc:
            profile["parse_status"] = "failed"
            profile["parse_error"] = f"{type(exc).__name__}: {exc}"
            resume.profile = profile
            session.commit()
            return {"resume_id": resume_id, "status": "failed", "error": profile["parse_error"]}

        parsed["parse_status"] = "done"
        resume.profile = parsed
        resume.lang = parsed.get("lang", "zh")
        session.commit()
        matched = refresh_matches(session, resume)
        return {
            "resume_id": resume_id,
            "status": "ok",
            "source": parsed.get("source"),
            "skills": len(parsed.get("skills") or []),
            "matched": matched,
        }


@celery_app.task(name="app.workers.celery_app.run_match_task")
def run_match_task(resume_id: int) -> dict:
    """画像修改后异步重算（§9）：按 resume_id 重建 match_scores（先删后插，幂等）。"""
    from app.core.db import SessionLocal
    from app.models import Resume
    from app.pipelines.match import refresh_matches

    with SessionLocal() as session:
        resume = session.get(Resume, resume_id)
        if resume is None:
            return {"resume_id": resume_id, "error": "resume not found"}
        return {"resume_id": resume_id, "status": "ok", "matched": refresh_matches(session, resume)}


@celery_app.task(name="app.workers.celery_app.interview_reminder_task")
def interview_reminder_task(within_days: int | None = None) -> dict:
    """面试催进（§12.6 P5 ②）：临近/刚过未更新结果的面试 → 邮件/IM/日志。

    邮件与 IM 通道复用 notify.py（未配置则降级打日志），与投递催进 daily 09:00 分时，
    避免两类提醒挤在同一封/同一条消息里。
    """
    from app.core.db import SessionLocal
    from app.services.interview import DEFAULT_WITHIN_DAYS, build_interview_body, scan_interview_reminders
    from app.services.notify import is_configured, is_im_configured, send_im_text, send_mail

    with SessionLocal() as session:
        items = scan_interview_reminders(session, within_days=within_days or DEFAULT_WITHIN_DAYS)
        if not items:
            return {"total": 0, "mailed": False, "im_sent": False}
        body = build_interview_body(items)
        mailed = send_mail(f"【简历直达】面试提醒：{len(items)} 场待准备", body) if is_configured() else False
        im_sent = send_im_text(body) if is_im_configured() else False
        if not mailed and not im_sent:
            for it in items:  # 邮件/IM 均未配置：降级打日志
                print(
                    f"[interview] application #{it['application_id']} "
                    f"{it['job_title']!r} 面试 {it['interview_at']}（days_left={it['days_left']}）"
                    f" -> {it['apply_url']}"
                )
        return {"total": len(items), "mailed": mailed, "im_sent": im_sent}


@celery_app.task(name="app.workers.celery_app.auto_tune_weights_task")
def auto_tune_weights_task(apply: bool | None = None) -> dict:
    """§12.5 反馈回灌闭环：反馈 → 调参 → 采纳（样本与增益双门槛，低于门槛不换参数）。

    WEIGHTS_AUTO_TUNE=false（默认）时直接跳过：beat 条目也不会注册；
    传 apply=True 可手动强制跑一次（如运维临时评估），但自动采纳的门槛仍在。
    """
    from app.core.db import SessionLocal
    from app.services.weights import auto_tune

    effective = settings.weights_auto_tune if apply is None else bool(apply)
    if not effective:
        return {"skipped": True, "reason": "weights_auto_tune_disabled"}
    with SessionLocal() as session:
        return auto_tune(session, apply=True)
