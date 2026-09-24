"""生产配置审计（§14 ⑤）：把"上线前该检查的配置项"变成可执行的检查。

设计：`audit_config` 是**纯函数**——配置以 Settings 实例或 dict 传入，`.env` 是否被 git
跟踪、`.env.example` 正文由调用方（`scripts/audit_production_config.py`）取好再传。
好处是单测不必改本机 `.env`，也不会因为本机配了真 key 而误判。

分级（fail 阻断上线 / warn 需人工确认 / info 仅告知）：
- fail：密钥为占位或过短、声明了 SMTP/IM 却缺必填项、`.env` 被 git 跟踪；
- warn：仍在 SQLite、未启用 Redis、采集 UA 还留着默认示例邮箱、SMTP 缺收发件人；
- info：密钥轮换清单（人工在服务商侧操作，代码只能列清单与判长度）。

**密钥值本身绝不打印、绝不入库**：本模块只判长度/命中占位词，不返回原文。
"""

from __future__ import annotations

from app.core.config import Settings

# 需要按密钥对待的字段（判长度与占位词）；smtp_user/notify_email 等非密钥字段不在此列
SECRET_FIELDS = ("llm_api_key", "ocr_api_key", "smtp_password", "im_webhook_secret")

# 密钥最短长度：短于此值几乎不可能是服务商真实签发的密钥
MIN_SECRET_LEN = 16

# 占位词（大小写不敏感）：命中即视为"还没填真值"
PLACEHOLDER_MARKERS = (
    "changeme",
    "change-me",
    "your-",
    "your_",
    "xxx",
    "todo",
    "placeholder",
    "example.com",
    "<",
)

# 默认 UA 里的示例邮箱：生产必须替换成真实联系地址（否则被站点当成匿名爬虫）
DEFAULT_UA_MARKER = "dev@example.com"

# 轮换清单：上线前逐项在服务商侧作废旧值、签发新值（代码只能提醒，无法代做）
ROTATION_KEYS: list[dict] = [
    {"field": "llm_api_key", "why": "简历解析/职位打标调用外部 LLM，泄露可被盗刷额度"},
    {"field": "ocr_api_key", "why": "扫描件 OCR 调用外部视觉接口，同为计费密钥"},
    {"field": "smtp_password", "why": "SMTP 授权码等同于发件邮箱的发送权"},
    {"field": "im_webhook_secret", "why": "钉钉加签 secret 泄露可被伪造群消息"},
    {"field": "DATABASE_URL", "why": "生产库口令随 URL 下发，需与开发库口令分离"},
    {"field": "REDIS_URL", "why": "broker 承载任务与结果，生产必须设密码并禁止公网暴露"},
]


def _value(config, name: str):
    if isinstance(config, dict):
        return config.get(name)
    return getattr(config, name, None)


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _looks_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def _secret_checks(config) -> list[dict]:
    """已填写的密钥判长度与占位词；**未填的密钥不算 fail**（功能开关本就允许留空）。"""
    checks: list[dict] = []
    for field in SECRET_FIELDS:
        value = _text(_value(config, field))
        if not value:
            continue  # 留空 = 该功能未启用，不是配置错误
        if _looks_placeholder(value):
            checks.append(
                {
                    "level": "fail",
                    "code": f"placeholder_{field}",
                    "message": f"{field} 疑似占位值（命中占位词），上线前必须替换为服务商签发的真实密钥",
                }
            )
        elif len(value) < MIN_SECRET_LEN:
            checks.append(
                {
                    "level": "fail",
                    "code": f"short_{field}",
                    "message": f"{field} 长度 {len(value)} 短于 {MIN_SECRET_LEN}，疑似占位或测试值",
                }
            )
    return checks


def _notify_checks(config) -> list[dict]:
    """通知通道的"半配置"是上线常见事故：声明了通道却缺必填项，静默不发反而更难查。"""
    checks: list[dict] = []
    smtp_host = _text(_value(config, "smtp_host"))
    if smtp_host:
        if not _text(_value(config, "smtp_password")):
            checks.append(
                {
                    "level": "fail",
                    "code": "smtp_password_missing",
                    "message": f"smtp_host={smtp_host} 已配置但 smtp_password 为空，邮件会静默不发",
                }
            )
        if not _text(_value(config, "smtp_user")) or not _text(_value(config, "notify_email")):
            checks.append(
                {
                    "level": "warn",
                    "code": "smtp_addresses_missing",
                    "message": "smtp_user / notify_email 未同时填写，催进邮件可能发不出去或无人接收",
                }
            )

    if _text(_value(config, "im_webhook_type")) and not _text(_value(config, "im_webhook_url")):
        checks.append(
            {
                "level": "fail",
                "code": "im_webhook_url_missing",
                "message": "im_webhook_type 已配置但 im_webhook_url 为空，IM 推送会静默不发",
            }
        )
    return checks


def _infra_checks(config) -> list[dict]:
    checks: list[dict] = []
    database_url = _text(_value(config, "database_url"))
    if database_url.startswith("sqlite"):
        checks.append(
            {
                "level": "warn",
                "code": "sqlite_in_production",
                "message": f"DATABASE_URL 仍是 SQLite（{database_url}），生产应切 PostgreSQL",
            }
        )

    if not _text(_value(config, "redis_url")):
        checks.append(
            {
                "level": "warn",
                "code": "redis_disabled",
                "message": "REDIS_URL 为空：限流退化为 DB 守卫、别名缓存直查 DB，Celery worker/beat 无法运行",
            }
        )

    if DEFAULT_UA_MARKER in _text(_value(config, "ua")):
        checks.append(
            {
                "level": "warn",
                "code": "default_ua_contact",
                "message": f"采集 UA 仍含默认示例邮箱 {DEFAULT_UA_MARKER}，生产需替换为真实联系地址",
            }
        )
    return checks


def _example_checks(env_example_text: str) -> list[dict]:
    """`.env.example` 必须列出全部密钥项，否则新人部署会漏配（漏配即静默降级）。"""
    if not env_example_text:
        return []
    checks: list[dict] = []
    for field in SECRET_FIELDS:
        if field.upper() not in env_example_text:
            checks.append(
                {
                    "level": "warn",
                    "code": f"example_missing_{field}",
                    "message": f".env.example 未列出 {field.upper()}，部署时容易漏配",
                }
            )
    return checks


def audit_config(config: Settings | dict, env_tracked: bool = False, env_example_text: str = "") -> dict:
    """跑全部配置审计，返回 {status, counts, checks, rotation, notes}。

    status = fail（有阻断项）> warn（有需确认项）> ok；`checks` 按 fail → warn → info 排列。
    """
    checks: list[dict] = []

    if env_tracked:
        checks.append(
            {
                "level": "fail",
                "code": "env_tracked_by_git",
                "message": "backend/.env 被 git 跟踪：密钥进了版本库，必须 git rm --cached 并轮换全部密钥",
            }
        )

    checks += _secret_checks(config)
    checks += _notify_checks(config)
    checks += _infra_checks(config)
    checks += _example_checks(env_example_text)

    order = {"fail": 0, "warn": 1, "info": 2}
    checks.sort(key=lambda c: order.get(c["level"], 3))

    counts = {level: sum(1 for c in checks if c["level"] == level) for level in ("fail", "warn", "info")}
    status = "fail" if counts["fail"] else ("warn" if counts["warn"] else "ok")
    return {
        "status": status,
        "counts": counts,
        "checks": checks,
        "rotation": ROTATION_KEYS,
        "notes": [
            "本审计只判密钥的**长度与占位词**，不打印密钥原文，也不改动任何配置。",
            "未填写的密钥不算 fail：留空表示该功能未启用（如不配 OCR 就不支持扫描件）。",
            "轮换清单需人工在服务商侧操作（作废旧值 → 签发新值 → 写入生产环境变量），代码无法代做。",
        ],
    }