"""投递闭环（P3 §8）：状态机 + 催进扫描。

状态迁移（只允许合法边，非法迁移抛 ValueError）：
    submitted → under_review → interview → offer
        │            │
        └────────────┴──> rejected / no_feedback / closed
    interview 还可直接 rejected / closed（一面挂/流程终止）。

每次状态变更写 feedback_log（outcome 取 interview/offer/rejected/no_feedback；
under_review/closed 只记 note），供后续反馈回灌调参（§7 反馈回灌）。

催进：submitted/under_review 卡超过 T 个自然日（settings.reminder_after_days，
默认 3）→ 出现在 /api/reminders 结果里（平台内提醒，不发第三方邮件）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Application, FeedbackLog, Job

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "submitted": {"under_review", "rejected", "no_feedback", "closed"},
    "under_review": {"interview", "rejected", "no_feedback", "closed"},
    "interview": {"offer", "rejected", "closed"},
    "offer": set(),
    "rejected": set(),
    "no_feedback": set(),
    "closed": set(),
}

# 写入 feedback_log.outcome 的状态（与 §3.6 口径一致）
OUTCOME_STATUSES = {"interview", "rejected", "no_feedback", "offer"}
# 催进扫描覆盖的状态
PENDING_STATUSES = ("submitted", "under_review")


class IllegalTransition(ValueError):
    pass


def transition(session: Session, application: Application, new_status: str, note: str | None = None) -> Application:
    """合法迁移 + feedback_log 回灌；返回更新后的 application。"""
    if new_status not in ALLOWED_TRANSITIONS.get(application.status, set()):
        raise IllegalTransition(
            f"非法状态迁移: {application.status} -> {new_status}"
            f"（允许: {sorted(ALLOWED_TRANSITIONS.get(application.status, set())) or '无，终态'}）"
        )
    application.status = new_status
    application.updated_at = datetime.utcnow()
    session.add(
        FeedbackLog(
            application_id=application.id,
            outcome=new_status if new_status in OUTCOME_STATUSES else None,
            note=note,
        )
    )
    session.commit()
    return application


def scan_reminders(session: Session, now: datetime | None = None, user_id: int | None = None) -> list[dict]:
    """催进扫描：pending 状态卡超过 T 天的申请，按卡住时长降序。"""
    now = now or datetime.utcnow()
    threshold = now - timedelta(days=settings.reminder_after_days)
    stmt = (
        select(Application, Job)
        .join(Job, Application.job_id == Job.id)
        .where(Application.status.in_(PENDING_STATUSES), Application.updated_at <= threshold)
    )
    if user_id is not None:
        stmt = stmt.where(Application.user_id == user_id)
    rows = session.execute(stmt.order_by(Application.updated_at.asc())).all()
    return [
        {
            "application_id": app.id,
            "user_id": app.user_id,
            "job_id": job.id,
            "job_title": job.title,
            "apply_url": app.apply_url or job.apply_url,
            "status": app.status,
            "stuck_days": (now - app.updated_at).days,
            "created_at": str(app.created_at),
        }
        for app, job in rows
    ]
