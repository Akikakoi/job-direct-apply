"""§10 简历原件静态加密（AES-GCM）用例：格式、幂等、篡改检测、存量兼容与接口埋点。

重点覆盖两件容易"看着像做了其实没做"的事：
1. **密文真的不是明文**（盘子上的文件读不出简历内容），且**改一个字节就解不开**；
2. **存量明文文件不被搞坏**（无魔数 → 透传），否则一开开关历史文件全变垃圾。
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from app.core.config import settings
from app.services import crypto
from app.services.crypto import MAGIC, CryptoError


@pytest.fixture(autouse=True)
def _no_llm_key(monkeypatch):
    """封掉 LLM key：本机 .env 配了真 key 时，上传会真连 LLM（单用例 10s+ 且结果不确定）。

    与 test_resume_parse / test_legal 同一约定：走解析路径的用例一律离线跑。
    """
    monkeypatch.setattr(settings, "llm_api_key", "")


@pytest.fixture()
def keyed(monkeypatch):
    """开一个测试密钥（只改 Settings 对象，不动本机 .env）。"""
    key = crypto.new_key()
    monkeypatch.setattr(settings, "uploads_key", key)
    return key


# ---------- 密钥 ----------


def test_new_key_is_base64_of_32_bytes_and_random():
    key = crypto.new_key()
    assert len(base64.b64decode(key)) == 32
    assert crypto.new_key() != key


def test_load_key_none_when_unset_and_errors_when_malformed(monkeypatch):
    monkeypatch.setattr(settings, "uploads_key", "")
    assert crypto.load_key() is None
    assert crypto.enabled() is False

    for bad in ("not-base64!!", base64.b64encode(b"short").decode()):
        monkeypatch.setattr(settings, "uploads_key", bad)
        assert crypto.enabled() is True  # 配了就算开（非法密钥在校验时报错，不静默用明文）
        with pytest.raises(CryptoError):
            crypto.load_key()


# ---------- 加解密 ----------


def test_roundtrip_and_ciphertext_is_not_plaintext(keyed):
    plain = "张三 zhangsan@example.com 13800000000 后端五年".encode("utf-8")
    blob = crypto.encrypt_bytes(plain)
    assert crypto.is_encrypted(blob)
    assert blob.startswith(MAGIC)
    assert blob != plain
    assert plain not in blob  # 明文字节不出现在密文里
    assert crypto.decrypt_bytes(blob) == plain


def test_same_plaintext_encrypts_differently_each_time(keyed):
    plain = b"same resume bytes"
    first, second = crypto.encrypt_bytes(plain), crypto.encrypt_bytes(plain)
    assert first != second  # 每次换 nonce，避免"两份相同简历可被一眼认出"
    assert crypto.decrypt_bytes(first) == crypto.decrypt_bytes(second) == plain


def test_encrypt_is_idempotent(keyed):
    blob = crypto.encrypt_bytes(b"data")
    assert crypto.encrypt_bytes(blob) == blob  # 二次加密原样返回，不套娃


def test_legacy_plaintext_file_passes_through(keyed):
    """存量兼容：没有魔数的旧文件按明文读回（否则历史原件全部变垃圾）。"""
    legacy = b"%PDF-1.4 legacy resume"
    assert crypto.is_encrypted(legacy) is False
    assert crypto.decrypt_bytes(legacy) == legacy


def test_tampered_ciphertext_rejected(keyed):
    blob = bytearray(crypto.encrypt_bytes(b"resume content"))
    blob[-1] ^= 0x01  # 翻转最后一个字节（GCM tag 区）
    with pytest.raises(CryptoError):
        crypto.decrypt_bytes(bytes(blob))


def test_truncated_ciphertext_rejected(keyed):
    with pytest.raises(CryptoError):
        crypto.decrypt_bytes(MAGIC + b"\x00" * 5)


def test_wrong_key_rejected(monkeypatch, keyed):
    blob = crypto.encrypt_bytes(b"resume content")
    monkeypatch.setattr(settings, "uploads_key", crypto.new_key())
    with pytest.raises(CryptoError):
        crypto.decrypt_bytes(blob)


def test_crypto_error_message_never_leaks_key_or_content(keyed):
    blob = crypto.encrypt_bytes(b"secret resume body")
    bad = bytes(blob[:-1] + bytes([blob[-1] ^ 0xFF]))
    with pytest.raises(CryptoError) as exc:
        crypto.decrypt_bytes(bad)
    text = str(exc.value)
    assert keyed not in text and "secret resume body" not in text


# ---------- 文件级工具（存量加密 / 追溯解密） ----------


def test_encrypt_file_and_decrypt_file(tmp_path, keyed):
    target = tmp_path / "resume_1_1.txt"
    target.write_bytes(b"original resume")

    assert crypto.encrypt_file(target) is True
    assert crypto.is_encrypted(target.read_bytes())
    assert crypto.encrypt_file(target) is False  # 幂等

    out = crypto.decrypt_file(target)
    assert out.read_bytes() == b"original resume"
    assert crypto.is_encrypted(target.read_bytes())  # 原密文文件不动


# ---------- 接口埋点 ----------


def test_upload_stores_ciphertext_when_key_configured(client, session, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path))
    monkeypatch.setattr(settings, "uploads_key", crypto.new_key())
    body = "张三 zhangsan@example.com python backend 五年".encode("utf-8")
    resp = client.post("/api/resumes", files={"file": ("resume.txt", body, "text/plain")})
    assert resp.status_code == 200
    assert resp.json()["data"]["file_encrypted"] is True

    from app.models import Resume

    stored = Path(session.get(Resume, resp.json()["data"]["id"]).file_path)
    blob = stored.read_bytes()
    assert crypto.is_encrypted(blob)
    assert b"zhangsan@example.com" not in blob  # 盘上没有明文邮箱
    assert body == crypto.decrypt_bytes(blob)
    # 解析照常（用的是内存里的字节，不受落盘加密影响）
    assert "python" in (session.get(Resume, resp.json()["data"]["id"]).profile or {}).get("skills", [])


def test_upload_stays_plaintext_without_key(client, session, monkeypatch, tmp_path):
    """未配密钥 = 旧行为（明文落盘、响应标记 false），且 prodcheck 会 warn。"""
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path))
    monkeypatch.setattr(settings, "uploads_key", "")
    resp = client.post("/api/resumes", data={"raw_text": "python backend"})
    assert resp.status_code == 200
    assert resp.json()["data"]["file_encrypted"] is False


def test_delete_removes_encrypted_file(client, session, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path))
    monkeypatch.setattr(settings, "uploads_key", crypto.new_key())
    resp = client.post("/api/resumes", files={"file": ("resume.txt", b"python", "text/plain")})
    rid = resp.json()["data"]["id"]

    from app.models import Resume

    stored = Path(session.get(Resume, rid).file_path)
    assert stored.exists()
    deleted = client.delete(f"/api/resumes/{rid}")
    assert deleted.status_code == 200
    assert deleted.json()["data"]["file_removed"] is True
    assert not stored.exists()  # 密文原件同样随删除消失（§14 ①）
