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


# ---------- P4 IM webhook（钉钉/企微） ----------

from app.services.notify import (  # noqa: E402
    _dingtalk_signed_url,
    _im_payload,
    is_im_configured,
    send_reminder_im,
)


def _with_im(monkeypatch, type_: str = "dingtalk", secret: str = ""):
    monkeypatch.setattr(config.settings, "im_webhook_type", type_)
    monkeypatch.setattr(config.settings, "im_webhook_url", "https://example.com/webhook")
    monkeypatch.setattr(config.settings, "im_webhook_secret", secret)


def test_is_im_configured(monkeypatch):
    _with_im(monkeypatch)
    assert is_im_configured() is True
    monkeypatch.setattr(config.settings, "im_webhook_type", "")
    assert is_im_configured() is False


def test_dingtalk_sign_deterministic():
    url = _dingtalk_signed_url("https://oapi.dingtalk.com/robot/send?access_token=x", "SEC123", 1700000000000)
    assert "timestamp=1700000000000" in url
    assert "sign=" in url


def test_dingtalk_payload(monkeypatch):
    _with_im(monkeypatch, "dingtalk")
    payload = _im_payload(ITEMS)
    assert payload["msgtype"] == "text"
    assert "服务端开发工程师" in payload["text"]["content"]


def test_wecom_payload(monkeypatch):
    _with_im(monkeypatch, "wecom")
    payload = _im_payload(ITEMS)
    assert payload["msgtype"] == "markdown"
    assert "**催进提醒：1 个投递卡超 3 天**" in payload["markdown"]["content"]
    assert "https://example.com/apply/1" in payload["markdown"]["content"]


def test_send_im_success_and_reject(monkeypatch):
    import app.services.notify as notify_mod

    _with_im(monkeypatch, "wecom")
    captured = {}

    class FakeResp:
        def __init__(self, errcode):
            self._e = errcode

        @property
        def status_code(self):
            return 200

        def json(self):
            return {"errcode": self._e, "errmsg": "ok" if self._e == 0 else "reject"}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def post(self, url, json, timeout):
            captured["url"], captured["json"] = url, json
            return FakeResp(0)

    monkeypatch.setattr(notify_mod.httpx, "post", lambda url, json, timeout: FakeClient().post(url, json, timeout))
    assert send_reminder_im(ITEMS) is True
    assert captured["url"] == "https://example.com/webhook"

    monkeypatch.setattr(
        notify_mod.httpx,
        "post",
        lambda url, json, timeout: type("R", (), {"status_code": 200, "json": lambda self: {"errcode": 310000}})(),
    )
    assert send_reminder_im(ITEMS) is False  # 钉钉拒绝（关键词/签名错误）


def test_send_im_unconfigured_or_empty(monkeypatch):
    monkeypatch.setattr(config.settings, "im_webhook_type", "")
    assert send_reminder_im(ITEMS) is False
    _with_im(monkeypatch)
    assert send_reminder_im([]) is False
