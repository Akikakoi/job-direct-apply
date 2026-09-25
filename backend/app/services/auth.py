"""§4.3 账号体系：JWT（HS256）+ 口令散列（PBKDF2-SHA256）。

**零依赖**：只用标准库 `hmac` / `hashlib` / `base64` / `json`，不引入 PyJWT / passlib /
bcrypt——与 §7、§12.6 的一贯口径一致（纯 Python、离线可测、装包即能跑）。HS256 的签名
计算本来就是 HMAC-SHA256（PyJWT 做的也是这件事），自己实现只多一层 base64 与 JSON 编解码。

口径（安全底线，与 §10 一致）：
- 令牌与口令内容**绝不落日志、绝不出现在错误信息里**——接口层只回 `reason` 这个粗粒度原因；
- 口令散列用 PBKDF2-SHA256 + 每用户随机盐，比对走 `hmac.compare_digest`（常数时间）；
- `auth_secret` 为空/占位 = 开发默认值，生产由 §14 ⑤ 的审计判 `fail`。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from app.core.config import settings

ALG = "HS256"
TYPE_ACCESS = "access"
TYPE_REFRESH = "refresh"

# PBKDF2 迭代次数：OWASP 对 PBKDF2-SHA256 的推荐下限（≥60 万偏重，12 万是性能与强度的折中）
PBKDF2_ITERATIONS = 120_000
SALT_BYTES = 16
MIN_PASSWORD_LEN = 8

# 令牌原因码（接口层据此给 401 文案，不泄露签名/密钥细节）
REASON_MALFORMED = "malformed"
REASON_BAD_SIGNATURE = "bad_signature"
REASON_EXPIRED = "expired"
REASON_WRONG_TYPE = "wrong_type"
REASON_BAD_SUBJECT = "bad_subject"


class TokenError(Exception):
    """令牌校验失败。`reason` 是粗粒度原因码，可安全回给客户端。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ---------- base64url（无填充，JWT 用） ----------


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ---------- 口令散列 ----------


def hash_password(password: str) -> str:
    """`pbkdf2_sha256$<迭代>$<盐b64>$<散列b64>`——自描述格式，日后换算法可平滑迁移。"""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${_b64e(salt)}${_b64e(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    """常数时间比对；`stored` 格式非法（历史脏数据/被改动）一律判失败，不抛异常。"""
    if not stored:
        return False
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        return False
    try:
        iterations = int(parts[1])
        salt = _b64d(parts[2])
        expected = _b64d(parts[3])
    except (ValueError, TypeError):
        return False
    if iterations <= 0:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)


# ---------- JWT（HS256） ----------


def _secret() -> bytes:
    return (settings.auth_secret or "").encode("utf-8")


def _sign(signing_input: bytes) -> str:
    return _b64e(hmac.new(_secret(), signing_input, hashlib.sha256).digest())


def create_token(
    user_id: int,
    role: str = "user",
    *,
    typ: str = TYPE_ACCESS,
    ttl_s: int | None = None,
    now: int | None = None,
) -> str:
    """签发令牌。`ttl_s` 缺省按令牌类型取配置（access 分钟级 / refresh 天级）。"""
    issued = int(now if now is not None else time.time())
    if ttl_s is None:
        ttl_s = (
            settings.auth_access_ttl_min * 60
            if typ == TYPE_ACCESS
            else settings.auth_refresh_ttl_days * 24 * 3600
        )
    header = {"alg": ALG, "typ": "JWT"}
    payload = {"sub": str(user_id), "role": role, "typ": typ, "iat": issued, "exp": issued + int(ttl_s)}
    segments = [
        _b64e(json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")),
        _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")),
    ]
    signing_input = ".".join(segments).encode("ascii")
    return ".".join([*segments, _sign(signing_input)])


def decode_token(
    token: str | None, *, expected_typ: str | None = None, now: int | None = None
) -> dict:
    """校验并解出载荷；任何问题抛 `TokenError`（原因码见 REASON_* 常量）。

    校验顺序刻意固定：结构 → 算法 → **签名** → 类型 → 过期。先验签名再读声明，避免
    拿未验签的 `exp`/`typ` 做判断（顺序反了等于把校验交给攻击者）。
    """
    if not token or not isinstance(token, str):
        raise TokenError(REASON_MALFORMED)
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise TokenError(REASON_MALFORMED)
    header_seg, payload_seg, signature = parts
    try:
        header = json.loads(_b64d(header_seg))
        payload = json.loads(_b64d(payload_seg))
    except (ValueError, TypeError):
        raise TokenError(REASON_MALFORMED) from None
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise TokenError(REASON_MALFORMED)
    if header.get("alg") != ALG:
        # 只认 HS256：不实现 alg=none / RS256，免去算法混淆类攻击面
        raise TokenError(REASON_MALFORMED)
    if not hmac.compare_digest(_sign(f"{header_seg}.{payload_seg}".encode("ascii")), signature):
        raise TokenError(REASON_BAD_SIGNATURE)
    if expected_typ is not None and payload.get("typ") != expected_typ:
        # refresh 不能当 access 用，反之亦然（access 泄露面更大、TTL 更短）
        raise TokenError(REASON_WRONG_TYPE)
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp <= int(now if now is not None else time.time()):
        raise TokenError(REASON_EXPIRED)
    try:
        user_id = int(payload.get("sub"))
    except (TypeError, ValueError):
        raise TokenError(REASON_BAD_SUBJECT) from None
    if user_id <= 0:
        raise TokenError(REASON_BAD_SUBJECT)
    payload["user_id"] = user_id
    return payload


def issue_tokens(user_id: int, role: str = "user", *, now: int | None = None) -> dict:
    """登录/注册/刷新统一出口：一次给齐 access + refresh，前端只需存两个字段。"""
    access = create_token(user_id, role, typ=TYPE_ACCESS, now=now)
    refresh = create_token(user_id, role, typ=TYPE_REFRESH, now=now)
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "expires_in": settings.auth_access_ttl_min * 60,
    }


_DUMMY_SALT = b"\x00" * SALT_BYTES


def verify_password_or_dummy(password: str, stored: str | None) -> bool:
    """登录用入口：用户不存在时也跑一次同等开销的散列。

    直接 `verify_password(pw, None)` 会立刻返回 False，响应快很多——"快=邮箱未注册"
    就是一个可利用的枚举信号。这里用固定盐把开销补齐，返回值恒为 False。
    """
    if stored:
        return verify_password(password, stored)
    hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), _DUMMY_SALT, PBKDF2_ITERATIONS)
    return False


def new_secret() -> str:
    """生成一个够用的新签名密钥（供部署时写入环境变量，不落库、不落日志）。"""
    return secrets.token_urlsafe(48)
