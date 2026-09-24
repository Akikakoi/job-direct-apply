"""生产配置审计测试（§14 ⑤）：密钥占位/长度、通知通道半配置、环境与示例文件一致性。

全部离线且**不改本机 .env**：audit_config 是纯函数，配置以 dict 传入。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.core.config import Settings
from app.services.prodcheck import (
    MIN_SECRET_LEN,
    ROTATION_KEYS,
    SECRET_FIELDS,
    audit_config,
)

REPO = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO / "backend" / ".env.example"
GITIGNORE = REPO / ".gitignore"

# 一个"已经生产化"的配置：PostgreSQL + 带口令 Redis + 真值密钥（长度足够）
PROD_DICT = {
    "database_url": "postgresql+psycopg2://jda:secret@db.internal:5432/jda",
    "redis_url": "redis://:secret@redis.internal:6379/0",
    "ua": "job-direct-apply-bot/0.1 (compliant; contact: ops@job-direct-apply.cn)",
    "llm_api_key": "sk-" + "a" * 30,
    "ocr_api_key": "sk-" + "b" * 30,
    "smtp_host": "smtp.qq.com",
    "smtp_user": "bot@job-direct-apply.cn",
    "smtp_password": "c" * 20,
    "notify_email": "ops@job-direct-apply.cn",
    "im_webhook_type": "dingtalk",
    "im_webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=x",
    "im_webhook_secret": "d" * 20,
}


def _codes(report: dict) -> set[str]:
    return {c["code"] for c in report["checks"]}


def _full_example_text() -> str:
    """含全部密钥项的示例正文（等价仓库里的 .env.example，但不依赖其内容细节）。"""
    return "\n".join(f"{field.upper()}=" for field in SECRET_FIELDS)


# ---------- 基线 ----------


def test_fully_productionized_config_passes():
    report = audit_config(PROD_DICT, env_tracked=False, env_example_text=_full_example_text())
    assert report["status"] == "ok"
    assert report["counts"] == {"fail": 0, "warn": 0, "info": 0}
    assert report["checks"] == []


def test_audit_accepts_settings_instance():
    """真实 Settings 也是合法入参（脚本走的就是这条路径）。"""
    report = audit_config(Settings())
    assert report["status"] in ("warn", "fail")
    assert isinstance(report["counts"]["warn"], int)


def test_sqlite_and_disabled_redis_warn_only():
    report = audit_config({"database_url": "sqlite:///./dev.db", "redis_url": ""})
    assert report["status"] == "warn"
    assert {"sqlite_in_production", "redis_disabled"} <= _codes(report)
    assert report["counts"]["fail"] == 0


# ---------- 密钥 ----------


def test_empty_secrets_are_not_failures():
    """留空 = 该功能未启用，不是配置错误（否则没配 OCR 就上不了线）。"""
    report = audit_config({field: "" for field in SECRET_FIELDS})
    assert report["counts"]["fail"] == 0
    assert not {c for c in _codes(report) if c.endswith(tuple(SECRET_FIELDS))}


def test_placeholder_secret_fails():
    report = audit_config({"llm_api_key": "changeme-please-1234567"})
    assert report["counts"]["fail"] == 1
    assert "placeholder_llm_api_key" in _codes(report)


def test_short_secret_fails():
    short = "abc123"
    assert len(short) < MIN_SECRET_LEN
    report = audit_config({"smtp_password": short})
    assert "short_smtp_password" in _codes(report)
    assert report["status"] == "fail"


def test_report_never_echoes_secret_values():
    """安全底线：审计结论不得回显密钥原文（日志/前端都会拿到这个 dict）。"""
    secret = "sk-" + "z" * 40
    report = audit_config({"llm_api_key": secret})
    assert secret not in json.dumps(report, ensure_ascii=False)


# ---------- 通知通道半配置 ----------


def test_smtp_without_password_fails_and_missing_addresses_warn():
    report = audit_config({"smtp_host": "smtp.qq.com"})
    assert "smtp_password_missing" in _codes(report)
    assert "smtp_addresses_missing" in _codes(report)
    assert report["status"] == "fail"


def test_im_type_without_url_fails():
    report = audit_config({"im_webhook_type": "wecom"})
    assert "im_webhook_url_missing" in _codes(report)
    assert report["status"] == "fail"


def test_default_ua_contact_warns():
    """Settings 默认 UA 里的示例邮箱是上线前必须替换的一项。"""
    report = audit_config({"ua": Settings().ua})
    assert "default_ua_contact" in _codes(report)


# ---------- git 与示例文件 ----------


def test_tracked_env_file_fails_and_sorts_before_warnings():
    report = audit_config({"database_url": "sqlite:///./dev.db"}, env_tracked=True)
    assert report["status"] == "fail"
    assert "env_tracked_by_git" in _codes(report)
    assert report["checks"][0]["level"] == "fail"  # fail 排在 warn 前
    assert all(c["level"] != "info" for c in report["checks"])


def test_example_file_missing_secret_field_warns():
    report = audit_config(PROD_DICT, env_example_text="LLM_API_KEY=\nSMTP_PASSWORD=\n")
    assert "example_missing_ocr_api_key" in _codes(report)
    assert "example_missing_im_webhook_secret" in _codes(report)


def test_repo_env_example_documents_every_secret_field():
    """仓库里的 .env.example 必须列全密钥项（漏配即静默降级，部署时很难发现）。"""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    for field in SECRET_FIELDS:
        assert field.upper() in text, f".env.example 未列出 {field.upper()}"
    assert "DATABASE_URL" in text and "REDIS_URL" in text


def test_repo_gitignore_excludes_env_file():
    text = GITIGNORE.read_text(encoding="utf-8")
    ignored = {line.strip() for line in text.splitlines()}
    assert "backend/.env" in ignored and ".env" in ignored


def test_rotation_checklist_covers_key_and_infra_secrets():
    fields = {item["field"] for item in ROTATION_KEYS}
    assert {"llm_api_key", "smtp_password", "DATABASE_URL", "REDIS_URL"} <= fields
    assert all(item["why"] for item in ROTATION_KEYS)  # 每项都要说明为什么必须轮换