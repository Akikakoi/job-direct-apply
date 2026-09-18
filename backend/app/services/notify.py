"""催进邮件通知（P4 通知渠道一期）：标准库 smtplib，零新依赖。

配置（backend/.env）：
    SMTP_HOST=smtp.qq.com      # 空则不发邮件（beat 降级为日志）
    SMTP_PORT=465              # SSL
    SMTP_USER=xxx@qq.com       # 发件邮箱
    SMTP_PASSWORD=授权码        # QQ Mail 需在设置里开 SMTP 并拿授权码
    NOTIFY_EMAIL=xxx@qq.com    # 收件邮箱

合规：只发到用户自己的 NOTIFY_EMAIL，绝不代发第三方。
"""

from __future__ import annotations

import smtplib
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

from app.core.config import settings


def is_configured() -> bool:
    return bool(settings.smtp_host and settings.smtp_user and settings.smtp_password and settings.notify_email)


def build_reminder_body(items: list[dict]) -> str:
    lines = [f"催进提醒：{len(items)} 个投递申请卡住超过 {settings.reminder_after_days} 天", ""]
    for it in items:
        lines.append(
            f"- #{it['application_id']} {it['job_title']}（{it['status']}，已卡 {it['stuck_days']} 天）\n"
            f"  {it['apply_url']}"
        )
    lines.append("")
    lines.append("建议：去对应 ATS 后台查看进度，或在本平台更新投递状态。")
    return "\n".join(lines)


def send_reminder_mail(items: list[dict]) -> bool:
    """发送催进汇总邮件；未配置或失败返回 False（不抛出，不影响 beat）。"""
    if not items or not is_configured():
        return False
    msg = MIMEText(build_reminder_body(items), "plain", "utf-8")
    msg["Subject"] = Header(f"【简历直达】催进提醒：{len(items)} 个投递待跟进", "utf-8")
    msg["From"] = formataddr(("简历直达", settings.smtp_user))
    msg["To"] = settings.notify_email
    try:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30) as server:
            server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(settings.smtp_user, [settings.notify_email], msg.as_string())
        return True
    except Exception as exc:  # 网络/授权失败不影响主流程
        print(f"[notify] send failed: {type(exc).__name__}: {exc}")
        return False
