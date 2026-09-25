"""简历原件静态加密（§10 个人信息：应用层 AES-GCM）。

目标：**库里能查到的一切都不该等于"文件本身"**。简历原件是最敏感的一份数据（姓名/手机/
邮箱/完整经历），落盘时按应用层密钥加密——磁盘被拿走、快照/对象存储误配时拿到的不是明文。

口径与取舍：
- **AES-256-GCM**（认证加密：改一个字节就解不开，而不是"换个头还能读"）；
- 落盘格式 = 魔数 `JDAENC1` + 12 字节随机 nonce + 密文（GCM tag 附在密文尾）；
- **保留魔数是为了向后兼容**：没有魔数的文件按旧明文读回（`decrypt_bytes` 直接透传），
  所以存量文件不必先迁移就能继续被删除/追溯；
- 密钥只从环境变量 `UPLOADS_KEY`（base64 的 32 字节）读，**不入库、不落日志、不回显**；
  未配置 = 不加密（旧行为；`prodcheck` 会 warn「原件明文落盘」）；
- 不对接 KMS：本项目是单机单用户形态，托管密钥属部署侧后续项（隐私政策已如实披露）。
"""

from __future__ import annotations

import base64
import secrets
from pathlib import Path

try:  # 缺库时不影响"未配置密钥"的部署：只在真的要加解密时才报错
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # pragma: no cover - 正常部署会装 cryptography
    AESGCM = None  # type: ignore[assignment]

from app.core.config import settings

MAGIC = b"JDAENC1"
NONCE_BYTES = 12
KEY_BYTES = 32


class CryptoError(Exception):
    """加解密失败（密钥缺失/格式错/密文被改动）。文案不含密钥或密文内容。"""


def new_key() -> str:
    """生成一个 base64 的 32 字节密钥（供部署时写入环境变量，不落库、不落日志）。"""
    return base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode("ascii")


def _decode_key(raw: str) -> bytes:
    try:
        key = base64.b64decode(raw.strip() + "=" * (-len(raw.strip()) % 4), validate=True)
    except Exception as exc:  # binascii.Error / ValueError
        raise CryptoError("UPLOADS_KEY 不是合法的 base64") from exc
    if len(key) != KEY_BYTES:
        raise CryptoError(f"UPLOADS_KEY 解码后应为 {KEY_BYTES} 字节，实际 {len(key)}")
    return key


def load_key() -> bytes | None:
    """读取并校验密钥；未配置返回 None（= 不加密），配置了但非法则报错（不静默用明文）。"""
    raw = (settings.uploads_key or "").strip()
    if not raw:
        return None
    return _decode_key(raw)


def enabled() -> bool:
    """是否开启静态加密（只判"有没有配置密钥"，非法密钥会在真正加解密时报错）。"""
    return bool((settings.uploads_key or "").strip())


def _aesgcm() -> "AESGCM":
    if AESGCM is None:  # pragma: no cover
        raise CryptoError("未安装 cryptography，无法加解密（pip install cryptography）")
    key = load_key()
    if key is None:
        raise CryptoError("UPLOADS_KEY 未配置，无法加解密")
    return AESGCM(key)


def is_encrypted(blob: bytes) -> bool:
    return blob.startswith(MAGIC)


def encrypt_bytes(data: bytes) -> bytes:
    """加密并加上魔数头；每次都换新 nonce（同一份文件两次加密结果不同）。"""
    if is_encrypted(data):
        return data  # 幂等：已经是密文就原样返回，避免二次加密
    nonce = secrets.token_bytes(NONCE_BYTES)
    return MAGIC + nonce + _aesgcm().encrypt(nonce, data, MAGIC)


def decrypt_bytes(blob: bytes) -> bytes:
    """解密；没有魔数的旧文件按明文透传（存量兼容）。密文被改动/密钥不对 → CryptoError。"""
    if not is_encrypted(blob):
        return blob
    body = blob[len(MAGIC) :]
    if len(body) <= NONCE_BYTES:
        raise CryptoError("密文长度不足，文件可能已损坏")
    nonce, ciphertext = body[:NONCE_BYTES], body[NONCE_BYTES:]
    try:
        return _aesgcm().decrypt(nonce, ciphertext, MAGIC)
    except Exception as exc:  # InvalidTag 等
        raise CryptoError("解密失败（密钥不匹配或文件已被改动）") from exc


def encrypt_file(path: str | Path) -> bool:
    """就地加密单个文件；返回是否真的写了盘（已是密文则 False，天然幂等）。"""
    target = Path(path)
    blob = target.read_bytes()
    if is_encrypted(blob):
        return False
    target.write_bytes(encrypt_bytes(blob))
    return True


def decrypt_file(path: str | Path, out: str | Path | None = None) -> Path:
    """解密到 `out`（缺省在同目录加 `.plain` 后缀）；不改动原密文文件。"""
    target = Path(path)
    plain = decrypt_bytes(target.read_bytes())
    dest = Path(out) if out is not None else target.with_suffix(target.suffix + ".plain")
    dest.write_bytes(plain)
    return dest
