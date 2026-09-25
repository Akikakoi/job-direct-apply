"""§4.3 账号体系（JWT 鉴权）用例：令牌签发/校验、注册登录刷新、越权拒绝。

零依赖 JWT 要单独覆盖「结构 / 算法 / 签名 / 类型 / 过期」五条校验分支——这些分支写错
不会报错，只会安静地把坏令牌当好令牌放行，正好是静态断言最值钱的地方。同理，越权用例
的价值在于：**默认关鉴权时旧行为不变**，开了才拦，且拦的是服务端而不是靠前端不传。
"""

from __future__ import annotations

import json
import time

import pytest

from app.core.config import settings
from app.models import Application, Company, Job, Resume, User
from app.services.auth import (
    PBKDF2_ITERATIONS,
    TokenError,
    _b64e,
    _b64d,
    create_token,
    decode_token,
    hash_password,
    verify_password,
    verify_password_or_dummy,
)


# ---------- 夹具与工具 ----------


@pytest.fixture(autouse=True)
def _no_llm_key(monkeypatch):
    """封掉 LLM key：本机 .env 配了真 key 时，走解析路径的用例会真连 LLM（慢且不确定）。

    与 test_resume_parse / test_legal 同一约定。
    """
    monkeypatch.setattr(settings, "llm_api_key", "")


def _make_user(session, email="a@example.com", password="secret123", role="user") -> User:
    user = User(email=email, password_hash=hash_password(password), role=role)
    session.add(user)
    session.commit()
    return user


def _token(user: User, role: str | None = None) -> str:
    return create_token(user.id, role or user.role)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_resume(session, user: User, active=True) -> Resume:
    resume = Resume(user_id=user.id, raw_text="python backend", profile={"skills": ["python"]}, is_active=active)
    session.add(resume)
    session.commit()
    return resume


def _make_job(session, external_id="ext-1") -> Job:
    job = Job(external_id=external_id, title="Backend Engineer", apply_url="https://example.com/a", status="active")
    session.add(job)
    session.commit()
    return job


# ---------- 口令散列 ----------


def test_password_hash_and_verify_roundtrip():
    stored = hash_password("correct horse")
    assert stored.startswith(f"pbkdf2_sha256${PBKDF2_ITERATIONS}$")
    assert stored.count("$") == 3  # 自描述格式：算法$迭代$盐$散列
    assert verify_password("correct horse", stored) is True
    assert verify_password("wrong horse", stored) is False
    # 同一口令两次散列必须不同（每次随机盐）
    assert hash_password("correct horse") != stored


def test_password_verify_rejects_malformed_stored_value():
    for bad in (None, "", "plain-text", "pbkdf2_sha256$abc$def", "md5$1$s$h", "pbkdf2_sha256$0$s$h"):
        assert verify_password("whatever", bad) is False


def test_verify_password_or_dummy_never_raises_and_is_false():
    """用户不存在时也要跑一次散列（响应时间不泄露邮箱是否注册），返回值恒为 False。"""
    assert verify_password_or_dummy("whatever", None) is False
    assert verify_password_or_dummy("whatever", "") is False


# ---------- 令牌：签发与校验 ----------


def test_token_roundtrip_carries_subject_and_role():
    token = create_token(7, "admin")
    payload = decode_token(token, expected_typ="access")
    assert payload["user_id"] == 7
    assert payload["role"] == "admin"
    assert payload["typ"] == "access"
    assert payload["exp"] > payload["iat"]
    assert token.count(".") == 2  # header.payload.signature


def test_expired_token_rejected():
    token = create_token(1, ttl_s=60, now=int(time.time()) - 3600)
    with pytest.raises(TokenError) as exc:
        decode_token(token)
    assert exc.value.reason == "expired"


def test_tampered_payload_rejected():
    token = create_token(1)
    header, _, signature = token.split(".")
    forged = _b64e(json.dumps({"sub": "99", "role": "admin", "typ": "access", "iat": 1, "exp": 9_999_999_999}).encode())
    with pytest.raises(TokenError) as exc:
        decode_token(f"{header}.{forged}.{signature}")
    assert exc.value.reason == "bad_signature"


def test_wrong_token_type_rejected():
    access = create_token(1, typ="access")
    with pytest.raises(TokenError) as exc:
        decode_token(access, expected_typ="refresh")
    assert exc.value.reason == "wrong_type"


def test_malformed_and_non_hs256_rejected():
    for bad in (None, "", "abc", "a.b", "a.b.c.d", "not.a.token"):
        with pytest.raises(TokenError) as exc:
            decode_token(bad)
        assert exc.value.reason == "malformed"

    # alg=none 伪造（经典降级攻击）：只认 HS256，别的算法一律拒收
    header = _b64e(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = _b64e(json.dumps({"sub": "1", "typ": "access", "exp": 9_999_999_999}).encode())
    with pytest.raises(TokenError) as exc:
        decode_token(f"{header}.{payload}.")
    assert exc.value.reason == "malformed"


def test_non_positive_subject_rejected():
    token = create_token(-1)
    with pytest.raises(TokenError) as exc:
        decode_token(token)
    assert exc.value.reason == "bad_subject"


# ---------- 注册 / 登录 / 刷新 ----------


def test_register_returns_token_pair_and_always_user_role(client, session):
    resp = client.post("/api/auth/register", json={"email": "New@Example.com", "password": "secret123"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["email"] == "new@example.com"  # 入库归一化小写
    assert data["role"] == "user"  # 自助注册不接受客户端指定角色
    assert data["token_type"] == "bearer"
    assert data["expires_in"] == settings.auth_access_ttl_min * 60
    assert decode_token(data["access_token"], expected_typ="access")["user_id"] == data["id"]
    # 注册返回的 access 令牌可直接用于登录态接口
    assert client.get("/api/auth/me", headers=_auth(data["access_token"])).status_code == 200


def test_register_duplicate_email_conflicts(client, session):
    _make_user(session, email="dup@example.com")
    resp = client.post("/api/auth/register", json={"email": "DUP@example.com", "password": "secret123"})
    assert resp.status_code == 409


def test_register_validation_rejects_short_password_and_bad_email(client):
    assert client.post("/api/auth/register", json={"email": "x@example.com", "password": "short"}).status_code == 422
    assert client.post("/api/auth/register", json={"email": "not-an-email", "password": "secret123"}).status_code == 422
    assert client.post("/api/auth/register", json={"email": "@example.com", "password": "secret123"}).status_code == 422


def test_login_success_and_failure_share_one_message(client, session):
    _make_user(session, email="login@example.com", password="secret123")
    ok = client.post("/api/auth/login", json={"email": "login@example.com", "password": "secret123"})
    assert ok.status_code == 200
    assert "access_token" in ok.json()["data"]

    wrong = client.post("/api/auth/login", json={"email": "login@example.com", "password": "nope"})
    unknown = client.post("/api/auth/login", json={"email": "ghost@example.com", "password": "nope"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]  # 不区分"未注册/口令错"


def test_me_requires_token_even_when_auth_optional(client, session):
    """/api/auth/me 是登录态自检接口：auth_required=off 时也必须带令牌。"""
    assert settings.auth_required is False
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer garbage"}).status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": "Basic abc"}).status_code == 401

    user = _make_user(session)
    assert client.get("/api/auth/me", headers=_auth(_token(user))).status_code == 200


def test_refresh_accepts_only_refresh_token(client, session):
    user = _make_user(session)
    access = create_token(user.id, typ="access")
    refresh = create_token(user.id, typ="refresh")

    assert client.post("/api/auth/refresh", json={"refresh_token": access}).status_code == 401
    assert client.post("/api/auth/refresh", json={"refresh_token": "junk"}).status_code == 401

    resp = client.post("/api/auth/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 200
    new_access = resp.json()["data"]["access_token"]
    assert decode_token(new_access, expected_typ="access")["user_id"] == user.id


def test_refresh_rejects_deleted_account(client, session):
    user = _make_user(session)
    refresh = create_token(user.id, typ="refresh")
    session.delete(user)
    session.commit()
    assert client.post("/api/auth/refresh", json={"refresh_token": refresh}).status_code == 401


# ---------- 鉴权开关与越权 ----------


def test_auth_required_blocks_user_endpoints(client, session, monkeypatch):
    monkeypatch.setattr(settings, "auth_required", True)
    user = _make_user(session)
    _make_resume(session, user)

    assert client.get("/api/resumes").status_code == 401
    assert client.get("/api/applications").status_code == 401
    assert client.get("/api/reminders").status_code == 401
    # 带着令牌就正常
    assert client.get("/api/resumes", headers=_auth(_token(user))).status_code == 200
    # 坏令牌不静默降级成匿名，直接 401（reason 写明原因）
    bad = client.get("/api/resumes", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401
    assert "malformed" in bad.json()["detail"]


def test_legacy_mode_without_token_keeps_old_behavior(client, session):
    """auth_required 默认关：老客户端（表单/查询参数传 user_id）行为完全不变。"""
    user = _make_user(session)
    _make_resume(session, user)
    resp = client.get(f"/api/resumes?user_id={user.id}")
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] == 1


def test_cross_user_resume_access_forbidden(client, session):
    alice = _make_user(session, email="alice@example.com")
    bob = _make_user(session, email="bob@example.com")
    bob_resume = _make_resume(session, bob)
    _make_resume(session, alice)

    headers = _auth(_token(alice))
    # 读他人简历：403（不是 404——资源存在但无权，语义要准确）
    assert client.get(f"/api/resumes/{bob_resume.id}", headers=headers).status_code == 403
    # 改画像 / 推荐 / 删除 / 优化 全部同样拦住
    assert client.put(f"/api/resumes/{bob_resume.id}/profile", json={"profile": {"skills": ["x"]}}, headers=headers).status_code == 403
    assert client.get(f"/api/recommend?resume_id={bob_resume.id}", headers=headers).status_code == 403
    assert client.delete(f"/api/resumes/{bob_resume.id}", headers=headers).status_code == 403
    assert client.post(f"/api/resumes/{bob_resume.id}/optimize", json={"job_id": 1}, headers=headers).status_code == 403
    # 列表只出自己那一份，不会因为不传 user_id 就返回全库
    listed = client.get("/api/resumes", headers=headers).json()["data"]
    assert listed["total"] == 1
    assert listed["items"][0]["id"] != bob_resume.id


def test_admin_token_can_cross_users(client, session):
    admin = _make_user(session, email="admin@example.com", role="admin")
    bob = _make_user(session, email="bob@example.com")
    bob_resume = _make_resume(session, bob)
    assert client.get(f"/api/resumes/{bob_resume.id}", headers=_auth(_token(admin))).status_code == 200


def test_create_application_scopes_user_id_to_token(client, session):
    """自述型 user_id 与令牌不符时以令牌为准（静默覆盖），并把越权拦在资源 id 上。"""
    alice = _make_user(session, email="alice@example.com")
    bob = _make_user(session, email="bob@example.com")
    job = _make_job(session)

    forged = client.post(
        "/api/applications",
        json={"user_id": bob.id, "job_id": job.id, "authorized": True},
        headers=_auth(_token(alice)),
    )
    assert forged.status_code == 200
    row = session.get(Application, forged.json()["data"]["id"])
    assert row.user_id == alice.id  # 冒名不成立：落库的是令牌主体

    ok = client.post(
        "/api/applications",
        json={"user_id": alice.id, "job_id": job.id, "authorized": True},
        headers=_auth(_token(alice)),
    )
    assert ok.status_code == 409  # 上面那条已在流程中

    # 按 id 取他人资源：状态变更 / 面试登记 / 陪伴包 / 帮填 全部 403
    app_id = row.id
    assert client.post(f"/api/applications/{app_id}/status", json={"status": "under_review"}, headers=_auth(_token(bob))).status_code == 403
    assert client.get(f"/api/applications/{app_id}/interview-kit", headers=_auth(_token(bob))).status_code == 403
    assert client.put(f"/api/applications/{app_id}/interview-at", json={"interview_at": None}, headers=_auth(_token(bob))).status_code == 403
    assert client.post(f"/api/applications/{app_id}/autofill", headers=_auth(_token(bob))).status_code != 200
    # 列表与催进也按令牌主体过滤
    assert client.get("/api/applications", headers=_auth(_token(bob))).json()["data"]["total"] == 0


def test_self_declared_user_id_is_overridden_not_rejected(client, session):
    """老客户端带着默认 user_id=1 也能用（静默归到令牌主体）——上线过渡期的真实诉求。"""
    alice = _make_user(session, email="alice@example.com")
    headers = _auth(_token(alice))
    resp = client.post(
        "/api/resumes",
        data={"raw_text": "python backend", "user_id": "1"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert session.get(Resume, resp.json()["data"]["id"]).user_id == alice.id
    # 列表即使传了别人的 user_id，也只出自己的
    assert client.get("/api/resumes?user_id=999", headers=headers).json()["data"]["total"] == 1


def test_admin_token_can_act_as_another_user(client, session):
    """admin 的显式 user_id 被尊重（代操作），普通用户则不行——两者的差别只在自述字段上。"""
    admin = _make_user(session, email="boss@example.com", role="admin")
    bob = _make_user(session, email="bob@example.com")
    job = _make_job(session, external_id="ext-admin")
    resp = client.post(
        "/api/applications",
        json={"user_id": bob.id, "job_id": job.id, "authorized": True},
        headers=_auth(_token(admin)),
    )
    assert resp.status_code == 200
    assert session.get(Application, resp.json()["data"]["id"]).user_id == bob.id


def test_company_admin_actions_require_admin(client, session, monkeypatch):
    """公司映射增删改属管理动作：开关关=老行为，开关开=必须 admin 令牌。"""
    body = {"slug": "acme", "name": "Acme", "ats_type": "greenhouse"}
    assert client.post("/api/companies", json=body).status_code == 200  # 开关关，旧行为

    monkeypatch.setattr(settings, "auth_required", True)
    assert client.post("/api/companies", json={**body, "slug": "b"}).status_code == 401
    user = _make_user(session)
    assert client.post("/api/companies", json={**body, "slug": "c"}, headers=_auth(_token(user))).status_code == 403
    admin = _make_user(session, email="boss@example.com", role="admin")
    assert client.post("/api/companies", json={**body, "slug": "d"}, headers=_auth(_token(admin))).status_code == 200
