"""催进邮件通知测试（P4）：mock smtplib，不触网、不真发信。"""

from __future__ import annotations

from app.core import config
from app.services.notify import build_reminder_body, is_configured, send_reminder_mail

ITEMS = [
    {
        "application_id": 1,
        "job_title": "服务端开发工程师",
        "status": "submitted",
        "stuck_days": 5,
        "apply_url": "https://example.com/apply/1",
    }
]


def _with_smtp(monkeypatch, **over):
    monkeypatch.setattr(config.settings, "smtp_host", over.get("host", "smtp.qq.com"))
    monkeypatch.setattr(config.settings, "smtp_port", 465)
    monkeypatch.setattr(config.settings, "smtp_user", over.get("user", "me@qq.com"))
    monkeypatch.setattr(config.settings, "smtp_password", over.get("pwd", "authcode"))
    monkeypatch.setattr(config.settings, "notify_email", over.get("to", "me@qq.com"))


def test_is_configured(monkeypatch):
    _with_smtp(monkeypatch)
    assert is_configured() is True
    monkeypatch.setattr(config.settings, "smtp_host", "")
    assert is_configured() is False


def test_build_body_contains_key_info():
    body = build_reminder_body(ITEMS)
    assert "1 个投递申请卡住超过 3 天" in body
    assert "服务端开发工程师" in body and "5 天" in body
    assert "https://example.com/apply/1" in body


def test_send_mail_success(monkeypatch):
    _with_smtp(monkeypatch)
    captured = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            captured["host"], captured["port"] = host, port

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, u, p):
            captured["login"] = u

        def sendmail(self, _from, to, raw):
            captured["to"], captured["raw"] = to, raw

    monkeypatch.setattr("app.services.notify.smtplib.SMTP_SSL", FakeSMTP)
    assert send_reminder_mail(ITEMS) is True
    assert captured["host"] == "smtp.qq.com" and captured["port"] == 465
    assert captured["to"] == ["me@qq.com"]
    # MIMEText 正文 base64 编码，需解码校验
    import email
    from email import policy

    msg = email.message_from_string(captured["raw"], policy=policy.default)
    body = msg.get_body(preferencelist=("plain",)).get_content()
    assert "服务端开发工程师" in body


def test_send_mail_failure_returns_false(monkeypatch):
    _with_smtp(monkeypatch)

    def boom(*a, **kw):
        raise ConnectionError("network down")

    monkeypatch.setattr("app.services.notify.smtplib.SMTP_SSL", boom)
    assert send_reminder_mail(ITEMS) is False  # 不抛出


def test_send_skips_when_unconfigured(monkeypatch):
    monkeypatch.setattr(config.settings, "smtp_host", "")
    assert send_reminder_mail(ITEMS) is False
    assert send_reminder_mail([]) is False  # 空列表也不发
