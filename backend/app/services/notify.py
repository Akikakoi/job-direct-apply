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

import httpx

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


def send_mail(subject: str, body: str) -> bool:
    """通用纯文本邮件（发给 settings.notify_email）；未配置或失败返回 False（不抛出）。

    §12.6 P5 ② 面试催进复用本函数——SMTP 细节只在这一处，提醒类文案由调用方生成。
    """
    if not is_configured():
        return False
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
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


def send_reminder_mail(items: list[dict]) -> bool:
    """发送催进汇总邮件；未配置或失败返回 False（不抛出，不影响 beat）。"""
    if not items:
        return False
    return send_mail(f"【简历直达】催进提醒：{len(items)} 个投递待跟进", build_reminder_body(items))


# ---------- P4 IM webhook（钉钉/企微群机器人，与邮件互相独立） ----------


def is_im_configured() -> bool:
    return bool(settings.im_webhook_type and settings.im_webhook_url)


def _dingtalk_signed_url(url: str, secret: str, timestamp_ms: int) -> str:
    """钉钉加签：HmacSHA256(secret, "{timestamp}\\n{secret}") → base64 → urlencode。"""
    import hashlib
    import hmac
    from base64 import b64encode
    from urllib.parse import quote_plus

    string_to_sign = f"{timestamp_ms}\n{secret}"
    sign = b64encode(hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha256).digest())
    return f"{url}&timestamp={timestamp_ms}&sign={quote_plus(sign)}"


def _im_payload(items: list[dict]) -> dict:
    if settings.im_webhook_type == "wecom":
        lines = [f"**催进提醒：{len(items)} 个投递卡超 {settings.reminder_after_days} 天**"]
        for it in items:
            lines.append(f"> #{it['application_id']} [{it['job_title']}]({it['apply_url']}) {it['status']}，卡 {it['stuck_days']} 天")
        return {"msgtype": "markdown", "markdown": {"content": "\n".join(lines)}}
    # 钉钉（默认）
    return {"msgtype": "text", "text": {"content": build_reminder_body(items)}}


def send_reminder_im(items: list[dict]) -> bool:
    """推送到钉钉/企微群机器人；未配置或失败返回 False（不抛出）。"""
    if not items:
        return False
    return _post_im(_im_payload(items))


def send_im_text(content: str) -> bool:
    """通用 IM 文本推送（§12.6 P5 ② 面试催进复用）；未配置或失败返回 False。"""
    if not content or not is_im_configured():
        return False
    if settings.im_webhook_type == "wecom":
        payload = {"msgtype": "markdown", "markdown": {"content": content}}
    else:  # 钉钉（默认）
        payload = {"msgtype": "text", "text": {"content": content}}
    return _post_im(payload)


def _post_im(payload: dict) -> bool:
    """IM webhook 统一投递（加签 + 错误码判定），两类提醒共用。"""
    url = settings.im_webhook_url
    if settings.im_webhook_type == "dingtalk" and settings.im_webhook_secret:
        import time as time_mod

        url = _dingtalk_signed_url(url, settings.im_webhook_secret, round(time_mod.time() * 1000))
    try:
        resp = httpx.post(url, json=payload, timeout=15)
        data = resp.json()
        ok = resp.status_code == 200 and int(data.get("errcode", 0)) == 0
        if not ok:
            print(f"[notify] im webhook rejected: {data}")
        return ok
    except Exception as exc:
        print(f"[notify] im send failed: {type(exc).__name__}: {exc}")
        return False
