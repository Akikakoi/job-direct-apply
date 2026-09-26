"""FastAPI 入口（P1：健康检查 + 职位列表；P2：简历上传解析；P3/P4/P5 投递、洞察、合规；§4.3 JWT 鉴权已接入）。"""

from __future__ import annotations

import hmac
import re
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.db import SessionLocal
from app.models import Application, Company, Job, MatchScore, Resume, User
from app.pipelines.match import refresh_matches
from app.pipelines.parse import parse_resume_text
from app.pipelines.rules import detect_lang
from app.pipelines.text_extract import UnsupportedFile, extract_text
from app.services import crypto, obs
from app.services.applications import IllegalTransition, scan_reminders, transition
from app.services.auth import (
    TYPE_ACCESS,
    TYPE_REFRESH,
    TokenError,
    decode_token,
    hash_password,
    issue_tokens,
    verify_password_or_dummy,
)
from app.services.city import city_match_variants


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


app = FastAPI(title="Job Direct Apply", version="0.1.0")

# §10 可观测性：JSON 日志（LOG_JSON=true 才开）、Sentry（配了 DSN 且装了 SDK 才初始化）
obs.setup_logging()
obs.init_sentry()
app.middleware("http")(obs.http_observability)


@app.get("/metrics")
def metrics(
    request: Request,
    token: str | None = Query(default=None, description="METRICS_TOKEN 配置后必填"),
    session: Session = Depends(get_session),
) -> Response:
    """Prometheus 抓取点（§10）：HTTP 指标 + 业务口径（复用 ops，不在抓取路径另算一套）。

    默认不强制凭据（Prometheus 抓取通常无凭据）；配了 `METRICS_TOKEN` 才要求同值令牌，
    可用 `?token=` 或 `Authorization: Bearer`。指标内容不含密钥与个人信息。
    """
    if not settings.metrics_enabled:
        raise HTTPException(status_code=404, detail="指标未启用（METRICS_ENABLED=false）")
    expected = (settings.metrics_token or "").strip()
    if expected:
        bearer = (request.headers.get("authorization") or "").strip()
        supplied = bearer.split()[-1] if bearer else (token or "")
        if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
            raise HTTPException(status_code=401, detail="指标访问令牌不正确")
    body = obs.prometheus_text(session=session)
    return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# ---------- §4.3 鉴权依赖（JWT / HS256；实现见 app/services/auth.py） ----------


def _bearer_token(authorization: str | None) -> str | None:
    """从 Authorization 头取 Bearer 令牌；非 Bearer 形式按「未携带」处理（兼容旧客户端）。"""
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1]


def current_user(authorization: str | None = Header(default=None)) -> dict | None:
    """当前登录用户（未携带令牌返回 None，是否 401 由 `settings.auth_required` 决定）。

    带了**坏令牌**一律 401 而不静默降级为匿名：客户端以为自己是登录态时，降级会变成
    "莫名看不见自己的数据/看到别人的数据"，说清楚原因（reason）比装没事更安全。
    """
    token = _bearer_token(authorization)
    if token is None:
        if settings.auth_required:
            raise HTTPException(status_code=401, detail="需要登录（缺少 Bearer 令牌）")
        return None
    try:
        payload = decode_token(token, expected_typ=TYPE_ACCESS)
    except TokenError as exc:
        raise HTTPException(status_code=401, detail=f"令牌无效（{exc.reason}）") from None
    return {"id": payload["user_id"], "role": payload.get("role") or "user"}


def _scope_user_id(requested: int | None, user: dict | None) -> int | None:
    """把**自述型** user_id（表单/查询参数/请求体）与令牌主体对齐。

    规则：有令牌时普通用户**以令牌为准（静默覆盖）**，admin 则尊重显式传值（可代操作）。
    这里不因"传了别人的 id"就 403——该字段表达的是"我以谁的身份提交"，不是资源标识：
    静默以令牌为准既绝了冒名，又不会让带着旧默认值（`user_id=1`）的老客户端登录后
    突然全部 403（上线过渡期的真实坑，冒烟时撞到过）。**按 id 取他人资源**的场景走
    `_owned`，那里才是 403。
    """
    if user is None:
        return requested
    if user.get("role") == "admin":
        return requested if requested is not None else user["id"]
    return user["id"]


def _owned(session: Session, model, obj_id: int, user: dict | None, what: str):
    """按 id 取用户态资源并做归属校验：不存在 404、非本人（且非 admin）403。"""
    row = session.get(model, obj_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"{what}不存在")
    if user is not None and row.user_id != user["id"] and user.get("role") != "admin":
        raise HTTPException(status_code=403, detail=f"越权访问他人{what}")
    return row


def _require_admin(user: dict | None) -> None:
    """管理动作（公司映射增删改）：未登录时沿用 auth_required 开关口径，登录则必须 admin。"""
    if user is None:
        if settings.auth_required:
            raise HTTPException(status_code=401, detail="需要登录（缺少 Bearer 令牌）")
        return
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")


# ---------- §4.3 账号体系（注册 / 登录 / 刷新 / 当前用户） ----------


class RegisterRequest(BaseModel):
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


def _user_out(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "role": user.role,
        "created_at": str(user.created_at),
    }


def _email_ok(email: str) -> bool:
    """极简邮箱校验：够拦住明显错输入即可（真实验证靠"注册后能否收到信"，不引校验库）。"""
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        return False
    local, _, domain = email.partition("@")
    return bool(local) and "." in domain and not domain.startswith(".") and not domain.endswith(".")


@app.post("/api/auth/register")
def register(body: RegisterRequest, session: Session = Depends(get_session)) -> dict:
    """注册并直接返回令牌对（免二次登录）。邮箱唯一（409）；口令过短/邮箱格式错 422。"""
    from app.services.auth import MIN_PASSWORD_LEN

    email = (body.email or "").strip().lower()
    if not _email_ok(email):
        raise HTTPException(status_code=422, detail="邮箱格式不正确")
    if len(body.password or "") < MIN_PASSWORD_LEN:
        raise HTTPException(status_code=422, detail=f"口令至少 {MIN_PASSWORD_LEN} 位")
    exists = session.execute(
        select(func.count()).select_from(User).where(func.lower(User.email) == email)
    ).scalar_one()
    if exists:
        raise HTTPException(status_code=409, detail="邮箱已注册")
    # 角色不接受客户端传入：自助注册一律 user，admin 由库/运维侧设置（否则等于自助提权）
    user = User(email=email, password_hash=hash_password(body.password), role="user")
    session.add(user)
    session.commit()
    return {
        "code": 0,
        "data": {**_user_out(user), **issue_tokens(user.id, user.role)},
        "message": "ok",
    }


@app.post("/api/auth/login")
def login(body: LoginRequest, session: Session = Depends(get_session)) -> dict:
    """邮箱 + 口令登录。失败一律 401 且文案不区分"邮箱不存在/口令错"（不给枚举口子）。"""
    email = (body.email or "").strip().lower()
    user = session.execute(select(User).where(func.lower(User.email) == email)).scalars().first()
    # 用户不存在时也走一次等开销散列（verify_password_or_dummy），响应时间不泄露注册状态
    if user is None or not verify_password_or_dummy(body.password or "", user.password_hash):
        raise HTTPException(status_code=401, detail="邮箱或口令不正确")
    return {
        "code": 0,
        "data": {**_user_out(user), **issue_tokens(user.id, user.role)},
        "message": "ok",
    }


@app.post("/api/auth/refresh")
def refresh_token(body: RefreshRequest, session: Session = Depends(get_session)) -> dict:
    """用 refresh 令牌换新令牌对。access 令牌不能当 refresh 用（typ 校验，401）。"""
    try:
        payload = decode_token(body.refresh_token, expected_typ=TYPE_REFRESH)
    except TokenError as exc:
        raise HTTPException(status_code=401, detail=f"刷新令牌无效（{exc.reason}）") from None
    user = session.get(User, payload["user_id"])
    if user is None:  # 签名有效但账号已删除：令牌不能"复活"账号
        raise HTTPException(status_code=401, detail="账号不存在")
    return {
        "code": 0,
        "data": {**_user_out(user), **issue_tokens(user.id, user.role)},
        "message": "ok",
    }


@app.get("/api/auth/me")
def me(
    user: dict | None = Depends(current_user), session: Session = Depends(get_session)
) -> dict:
    """当前用户信息（**恒需令牌**，与 auth_required 开关无关——此接口本就是登录态自检）。"""
    if user is None:
        raise HTTPException(status_code=401, detail="需要登录（缺少 Bearer 令牌）")
    row = session.get(User, user["id"])
    if row is None:
        raise HTTPException(status_code=401, detail="账号不存在")
    return {"code": 0, "data": _user_out(row), "message": "ok"}


@app.get("/api/jobs")
def list_jobs(
    city: str | None = None,
    source: str | None = None,
    limit: int = 50,
    offset: int = 0,
    session: Session = Depends(get_session),
) -> dict:
    stmt = select(Job).where(Job.status == "active")
    count_stmt = select(func.count()).select_from(Job).where(Job.status == "active")
    if city:
        # 城市规范化：contains 匹配，"杭州"命中"杭州市"、"New York"
        # 命中 "New York, NY" 与多城市串，别名 NYC→New York。
        # 双列匹配：优先规范化多城市索引串 city_keys（别名/多城市已在采集侧展开），
        # 未回填 city_keys 的旧行回退原 city 列。
        conds = [
            or_(
                func.lower(Job.city_keys).like(f"%{v}%", escape="\\"),
                func.lower(Job.city).like(f"%{v}%", escape="\\"),
            )
            for v in city_match_variants(city)
        ]
        if conds:
            stmt = stmt.where(or_(*conds))
            count_stmt = count_stmt.where(or_(*conds))
        else:
            stmt = stmt.where(Job.city.is_(None))
            count_stmt = count_stmt.where(Job.city.is_(None))
    if source:
        stmt = stmt.where(Job.source == source)
        count_stmt = count_stmt.where(Job.source == source)

    total = session.execute(count_stmt).scalar_one()
    rows = session.execute(stmt.order_by(Job.updated_at.desc()).limit(min(limit, 200)).offset(offset)).scalars().all()
    return {
        "code": 0,
        "data": {
            "total": total,
            "items": [
                {
                    "id": j.id,
                    "title": j.title,
                    "city": j.city,
                    "skills": j.skills,
                    "source": j.source,
                    "apply_url": j.apply_url,
                    "publish_date": str(j.publish_date) if j.publish_date else None,
                }
                for j in rows
            ],
        },
        "message": "ok",
    }


# ---------- P2 简历解析（§6） ----------

# profile 可人工修正的字段白名单（§12.3：profile 人工修正接口）
PROFILE_EDITABLE_KEYS = {
    "skills",
    "experience_years",
    "target_role",
    "cities",
    "salary_min",
    "salary_max",
    "uta_city",
    "industry",
    "edu_degree",
}


class ProfileUpdate(BaseModel):
    profile: dict


def _enqueue(task_name: str, *args) -> bool:
    """投递 Celery 任务（按任务名，避免 API 启动即加载 worker 模块）。

    broker/worker 不可用（开发期常无 Redis）返回 False，由调用方降级为请求内同步，
    保证「开了开关但没起 worker」不会静默丢任务（§9）。
    """
    try:
        from app.workers.celery_app import celery_app

        celery_app.send_task(task_name, args=list(args))
        return True
    except Exception:
        return False


def _activate_only(session: Session, user_id: int, keep_id: int) -> None:
    """同用户 active 互斥：除 keep_id 外全部取消 active。"""
    for other in session.execute(
        select(Resume).where(Resume.user_id == user_id, Resume.id != keep_id)
    ).scalars():
        other.is_active = False


def _parse_or_400(text: str, session: Session) -> dict:
    """解析简历；文本为空等用户输入问题转 400（LLM/网络异常已在管道内兜底）。"""
    try:
        return parse_resume_text(text, session)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/resumes")
async def upload_resume(
    file: UploadFile | None = File(default=None),
    raw_text: str | None = Form(default=None),
    user_id: int = Form(default=1),
    user: dict | None = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    """上传简历（txt/md/pdf 或直接贴文本），解析入库并返回 profile。

    这里只有"读请求体"是 async 的；文本抽取、密文落盘、LLM 解析全是阻塞调用，直接
    写在本函数里会把事件循环冻住几十秒（LLM 单次 20~35s 很常见），期间连 /health
    都没人应答，浏览器/代理的 keep-alive 连接空转被 RST，事件循环恢复后清理这些
    连接就会刷出 `ConnectionResetError: WinError 10054`（Windows Proactor 噪音）。
    所以读完 body 就把余下流程整段交给线程池，循环始终可服务其他请求。
    """
    user_id = _scope_user_id(user_id, user)
    file_bytes: bytes | None = None
    filename = "resume.txt"
    if file is not None:
        file_bytes = await file.read()
        filename = file.filename or filename
    elif not raw_text:
        raise HTTPException(status_code=400, detail="需要 file 或 raw_text 之一")
    return await run_in_threadpool(
        _ingest_resume, session, user_id, file_bytes, filename, raw_text
    )


def _ingest_resume(
    session: Session,
    user_id: int,
    file_bytes: bytes | None,
    filename: str,
    raw_text: str | None,
) -> dict:
    """上传后的同步主体（在线程池里跑）：抽文本 → 落盘 → 解析 → 入库。"""
    saved_path: str | None = None
    file_encrypted = False
    if file_bytes is not None:
        try:
            text = extract_text(filename, file_bytes)
        except UnsupportedFile as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # 原件存盘，便于追溯/重解析；配了 UPLOADS_KEY 则密文落盘（§10 静态加密）
        uploads = Path(settings.uploads_dir)
        uploads.mkdir(parents=True, exist_ok=True)
        suffix = "." + filename.rsplit(".", 1)[-1].lower()
        dest = uploads / f"resume_{user_id}_{int(datetime.now().timestamp())}{suffix}"
        file_encrypted = crypto.enabled()
        dest.write_bytes(crypto.encrypt_bytes(file_bytes) if file_encrypted else file_bytes)
        saved_path = str(dest)
    else:
        text = raw_text or ""

    # §9 resume_parse_task：开了异步开关时先入库占位（parse_status=pending），
    # 投递成功即返回，解析交给 worker；投递失败（无 Redis/worker）降级为请求内同步。
    pending = bool(settings.resume_parse_async)
    profile = {"parse_status": "pending"} if pending else _parse_or_400(text, session)

    resume = Resume(
        user_id=user_id,
        file_path=saved_path,
        raw_text=text,
        profile=profile,
        lang=profile.get("lang") or detect_lang(text),
        is_active=True,  # 新上传自动成为该用户当前生效简历
    )
    session.add(resume)
    session.flush()  # 拿到 id 后再做 active 互斥，避免把新简历自己也取消
    _activate_only(session, user_id, resume.id)
    session.commit()

    parse_status = profile.get("parse_status", "done")
    if pending and _enqueue("app.workers.celery_app.resume_parse_task", resume.id):
        return {
            "code": 0,
            "data": {
                "id": resume.id,
                "profile": resume.profile,
                "source": None,
                "is_active": True,
                "parse_status": "pending",
                "file_encrypted": file_encrypted,
            },
            "message": "ok",
        }
    if pending:  # 投递失败：同一行改同步解析，行为与未开开关一致
        profile = _parse_or_400(text, session)
        resume.profile = profile
        resume.lang = profile.get("lang") or resume.lang
        session.commit()
        parse_status = "done"

    return {
        "code": 0,
        "data": {
            "id": resume.id,
            "profile": resume.profile,
            "source": (resume.profile or {}).get("source"),
            "is_active": True,
            "parse_status": parse_status,
            "file_encrypted": file_encrypted,
        },
        "message": "ok",
    }


@app.get("/api/resumes")
def list_resumes(
    user_id: int | None = None,
    user: dict | None = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    # 登录态下列表恒按令牌主体过滤（不传 user_id 也不会看到全库简历）
    user_id = _scope_user_id(user_id, user)
    stmt = select(Resume)
    count_stmt = select(func.count()).select_from(Resume)
    if user_id is not None:
        stmt = stmt.where(Resume.user_id == user_id)
        count_stmt = count_stmt.where(Resume.user_id == user_id)
    total = session.execute(count_stmt).scalar_one()
    rows = session.execute(stmt.order_by(Resume.created_at.desc()).limit(100)).scalars().all()
    return {
        "code": 0,
        "data": {
            "total": total,
            "items": [
                {
                    "id": r.id,
                    "user_id": r.user_id,
                    "lang": r.lang,
                    "is_active": bool(r.is_active),
                    "source": (r.profile or {}).get("source"),
                    "skills": (r.profile or {}).get("skills") or [],
                    "created_at": str(r.created_at),
                }
                for r in rows
            ],
        },
        "message": "ok",
    }


@app.post("/api/resumes/{resume_id}/activate")
def activate_resume(
    resume_id: int,
    user: dict | None = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    """标记该简历为当前生效（同用户其他简历取消）。"""
    resume = _owned(session, Resume, resume_id, user, "简历")
    _activate_only(session, resume.user_id, resume.id)
    resume.is_active = True
    session.commit()
    return {"code": 0, "data": {"id": resume.id, "is_active": True}, "message": "ok"}


def _remove_upload_file(file_path: str | None) -> bool:
    """删除简历原件（§14 ① 个人信息删除：删库同时删盘，此前只删库行）。

    只删 uploads_dir 内的文件：file_path 来自库内，仍做归属校验——历史脏数据或
    人为改库可能指向目录外路径，不能因为"库里这么写"就删。删除失败（文件不存在/
    被占用）不抛异常：删库行是主诉求，残留文件不应让删除接口整体失败。
    """
    if not file_path:
        return False
    try:
        root = Path(settings.uploads_dir).resolve()
        target = Path(file_path).resolve()
        target.relative_to(root)  # 不在 uploads_dir 下 → ValueError，拒绝删除
    except (ValueError, OSError):
        return False
    try:
        target.unlink(missing_ok=True)
        return True
    except OSError:
        return False


@app.delete("/api/resumes/{resume_id}")
def delete_resume(
    resume_id: int,
    user: dict | None = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    """删除简历（连带 match_scores + 磁盘原件，§14 ① 个人信息删除口径）。"""
    resume = _owned(session, Resume, resume_id, user, "简历")
    session.execute(
        delete(MatchScore).where(MatchScore.resume_id == resume.id)
    )
    file_removed = _remove_upload_file(resume.file_path)
    session.delete(resume)
    session.commit()
    return {
        "code": 0,
        "data": {"id": resume_id, "deleted": True, "file_removed": file_removed},
        "message": "ok",
    }


@app.get("/api/resumes/{resume_id}")
def get_resume(
    resume_id: int,
    user: dict | None = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    resume = _owned(session, Resume, resume_id, user, "简历")
    return {
        "code": 0,
        "data": {
            "id": resume.id,
            "user_id": resume.user_id,
            "profile": resume.profile,
            "lang": resume.lang,
            "created_at": str(resume.created_at),
        },
        "message": "ok",
    }


@app.put("/api/resumes/{resume_id}/profile")
def update_resume_profile(
    resume_id: int,
    body: ProfileUpdate,
    user: dict | None = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    """人工修正 profile（白名单字段）；修改后触发重匹配在匹配引擎接入时生效。"""
    resume = _owned(session, Resume, resume_id, user, "简历")

    current = dict(resume.profile or {})
    for key, value in body.profile.items():
        if key in PROFILE_EDITABLE_KEYS:
            current[key] = value
    current["source"] = "manual"  # 人工修正标记，优先于解析结果
    current["parse_status"] = "done"  # 人工修正即视为解析终态
    resume.profile = current
    session.commit()
    # §12.3：profile 修改后触发重匹配；§9 run_match_task 异步开关命中则交给 worker
    if settings.match_async and _enqueue("app.workers.celery_app.run_match_task", resume.id):
        return {
            "code": 0,
            "data": {"id": resume.id, "profile": current, "matches_refreshed": 0, "async": True},
            "message": "ok",
        }
    refreshed = refresh_matches(session, resume)
    return {
        "code": 0,
        "data": {"id": resume.id, "profile": current, "matches_refreshed": refreshed},
        "message": "ok",
    }


# ---------- P2 推荐（§7） ----------


# 国内/海外判定依据：netease（网易）是国内源；其余 ATS 源（greenhouse/lever/
# ashby/workday/smartrecruiters）均为海外公司。实测库内该信号与城市信号完全一致
# （netease 2629 条全为中文城市，ATS 源 1918 条全为非中文城市），且比 city 正则
# 判定更可移植（不依赖 PG 专有正则，SQLite 同样可用）。新增国内源时在此登记。
DOMESTIC_SOURCES = ("netease",)


def _mix_regions(cn_rows: list, overseas_rows: list) -> list:
    """国内/海外两路交错合并（方案 A，§12.7 #9）。

    海外岗位此前被纯按 final_score 排序整体挤出默认列表（实测根因是"跨区城市
    恒 0 + role 跨语言难命中"，不是缺 experience_min/degree_req——§12.7 #9 已
    修正归因；该缺口由方案 C 收窄，交错仍是稳定曝光位的兜底）。两路各自已按分数
    有序，按 1:1 严格交错即可保证海外有稳定曝光位；首个位置给分数更高的一路，
    避免开头突兀。单区（region=cn/overseas）不走此逻辑。
    """
    if not cn_rows:
        return list(overseas_rows)
    if not overseas_rows:
        return list(cn_rows)

    def _score(row) -> float:
        ms = row[0]
        return float(ms.final_score if ms.final_score is not None else ms.rule_score or 0)

    merged: list = []
    i = j = 0
    turn_cn = _score(cn_rows[0]) >= _score(overseas_rows[0])
    while i < len(cn_rows) or j < len(overseas_rows):
        if (turn_cn and i < len(cn_rows)) or j >= len(overseas_rows):
            merged.append(cn_rows[i])
            i += 1
        else:
            merged.append(overseas_rows[j])
            j += 1
        turn_cn = not turn_cn
    return merged


def _round_robin(groups: list[list]) -> list:
    """多路严格轮转合并：逐轮每路各取一条，取空的路自动退出轮转。

    各路内部必须已按分数有序。`_mix_regions` 是它 N=2 的特例（那里还多一层
    "首位给分数更高的一路"的处理，这里靠调用方传参顺序表达）。
    """
    merged: list = []
    depth = 0
    while True:
        taken = False
        for group in groups:
            if depth < len(group):
                merged.append(group[depth])
                taken = True
        if not taken:
            return merged
        depth += 1


def _tokens(text: str) -> set[str]:
    """小写词元（只留数字/字母/CJK），供 role 与 title 的词面重合计分。"""
    return {t for t in re.split(r"[^0-9a-z\u4e00-\u9fff]+", (text or "").lower()) if t}


def _role_title_overlap(title_l: str, title_tokens: set[str], role_tokens: set[str]) -> int:
    """target_role 与职位 title 的词面重合数（L4 的第三档并列键）。

    ASCII 词按整词比；CJK 词按前两字比——"后端工程师" 与 "后端开发工程师" 整词
    不同但同指一个职能，取前两字才比得动。
    """
    n = 0
    for t in role_tokens:
        if t.isascii():
            n += 1 if t in title_tokens else 0
        else:
            n += 1 if t[:2] in title_l else 0
    return n


def _tie_break_key(row, role_tokens: set[str]) -> tuple:
    """并列打破键（L4，第二十五轮）。

    SQL 侧 order_by 只有 final_score → rule_score → updated_at，而前两级分数在海外
    场景大面积并列（实测前 10 名门槛上并列近百条），实际决定出场的退化成"谁先入库"
    （updated_at）。这里在分数之后补三档**分项真信号**：role 原始分 → skill 交集命中
    数 → title 与 target_role 的词面重合数；最后才轮到时间与 id 兜底。
    只改排序、不改任何分数，存量 match_scores 无需重算。键内全部取负 = 单次升序排序
    即得"分数高、信号强、时间新、id 小"序（datetime 不能取负，故换算成时间戳）。
    """
    ms, job = row
    expl = {e.get("key"): e for e in (ms.explain or []) if isinstance(e, dict)}
    role = float((expl.get("role") or {}).get("score") or 0.0)
    skill_hits = len((expl.get("skill") or {}).get("hit") or [])
    title_l = (job.title or "").lower()
    overlap = _role_title_overlap(title_l, _tokens(title_l), role_tokens)
    ts = job.updated_at.timestamp() if job.updated_at else 0.0
    return (
        -float(ms.final_score or 0.0),
        -float(ms.rule_score or 0.0),
        -role,
        -skill_hits,
        -overlap,
        -ts,
        job.id,
    )


def _route_rows(session: Session, where_clauses: list, fetch_n: int, target_role: str = "") -> list:
    """一路（国内/海外）的候选序列：先做公司级轮转，保证每家都有稳定曝光位。

    方案 A（`_mix_regions`）只解决了"国内 vs 海外"，没管"海外内部是谁"：这一路
    内部纯按 final_score 排时，高分公司会把窗口吃光——实测海外前 50 名 100% 来自
    ashby 上的 3 家公司（elevenlabs / linear / ashby），而 stripe(647)、
    equinox(736)、nvidia(40) 一条都进不来。故在路内再叠一层公司级轮转：公司顺序
    按"该公司在本简历下的最高分"降序（高分先出），公司内部仍按分数降序，逐轮各取
    一条。公司数是冷启动资产、个位数（当前库内 11 家），逐家取数的开销可忽略。

    第二十五轮补 L4：公司内、公司之间都补了并列打破键（`_tie_break_key`）——分数
    并列时按 role/skill/词面重合排序，最后才是 updated_at 与 id。
    """
    order = (MatchScore.final_score.desc(), MatchScore.rule_score.desc(), Job.updated_at.desc())
    best = func.max(MatchScore.final_score)
    best_rule = func.max(MatchScore.rule_score)
    company_ids = [
        row[0]
        for row in session.execute(
            select(Job.company_id)
            .select_from(MatchScore)
            .join(Job, MatchScore.job_id == Job.id)
            .where(*where_clauses)
            .group_by(Job.company_id)
            .order_by(best.desc(), best_rule.desc(), Job.company_id.asc())
        ).all()
    ]
    groups = [
        session.execute(
            select(MatchScore, Job)
            .join(Job, MatchScore.job_id == Job.id)
            .where(
                *where_clauses,
                Job.company_id.is_(None) if company_id is None else Job.company_id == company_id,
            )
            .order_by(*order)
            .limit(fetch_n)
        ).all()
        for company_id in company_ids
    ]
    role_tokens = _tokens(target_role)
    return _round_robin([sorted(g, key=lambda row: _tie_break_key(row, role_tokens)) for g in groups])


@app.get("/api/recommend")
def recommend(
    resume_id: int,
    limit: int = 20,
    offset: int = 0,
    refresh: bool = False,
    region: str = "all",  # all=全部 | cn=国内 | overseas=海外
    user: dict | None = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    """可解释排序推荐：读 match_scores 缓存；无缓存或 refresh=true 时全量重算。"""
    if region not in ("all", "cn", "overseas"):
        raise HTTPException(status_code=422, detail="region 仅支持 all|cn|overseas")

    resume = _owned(session, Resume, resume_id, user, "简历")

    cached = session.execute(
        select(func.count()).select_from(MatchScore).where(MatchScore.resume_id == resume.id)
    ).scalar_one()
    # 异步解析未完成（§9）：此时 profile 是空占位，重算只会写入中性分，跳过等 worker
    parsing = (resume.profile or {}).get("parse_status") == "pending"
    if not parsing and (refresh or cached == 0):
        refresh_matches(session, resume)

    where_clauses = [MatchScore.resume_id == resume.id, Job.status == "active"]
    if region == "cn":
        where_clauses.append(Job.source.in_(DOMESTIC_SOURCES))
    elif region == "overseas":
        # source 为 NULL 的脏数据归入海外，规避 NOT IN 遇 NULL 全部落空
        where_clauses.append(or_(Job.source.is_(None), Job.source.notin_(DOMESTIC_SOURCES)))

    total = session.execute(
        select(func.count())
        .select_from(MatchScore)
        .join(Job, MatchScore.job_id == Job.id)
        .where(*where_clauses)
    ).scalar_one()
    window = min(limit, 200)
    fetch_n = offset + window
    target_role = str((resume.profile or {}).get("target_role") or "")
    if region == "all":
        # 方案 A：两路各自做「公司级轮转」后再 1:1 交错，等价于全局交错排序的该窗口
        cn_rows = _route_rows(
            session, [*where_clauses, Job.source.in_(DOMESTIC_SOURCES)], fetch_n, target_role
        )
        overseas_rows = _route_rows(
            session,
            [*where_clauses, or_(Job.source.is_(None), Job.source.notin_(DOMESTIC_SOURCES))],
            fetch_n,
            target_role,
        )
        rows = _mix_regions(cn_rows, overseas_rows)[offset : offset + window]
    else:
        # 单区：where_clauses 已含 region 条件，路内做公司级轮转后切窗口
        rows = _route_rows(session, where_clauses, fetch_n, target_role)[offset : offset + window]
    return {
        "code": 0,
        "data": {
            "total": total,
            "items": [
                {
                    "job_id": job.id,
                    "title": job.title,
                    "city": job.city,
                    "skills": job.skills,
                    "source": job.source,
                    "apply_url": job.apply_url,
                    "score": float(ms.final_score) if ms.final_score is not None else float(ms.rule_score or 0),
                    "rule_score": float(ms.rule_score) if ms.rule_score is not None else None,
                    "vec_score": float(ms.vec_score) if ms.vec_score is not None else None,
                    "explain": ms.explain,
                }
                for ms, job in rows
            ],
        },
        "message": "ok",
    }


# ---------- P3 投递闭环（§8） ----------


class ApplicationCreate(BaseModel):
    user_id: int
    resume_id: int | None = None
    job_id: int
    authorized: bool  # 合规钩子：用户确认授权投递
    # §14 ② 同意留痕：前端投递确认弹层展示的政策版本（未带=服务端按当前版本补记）
    consent_version: str | None = None


class StatusUpdate(BaseModel):
    status: str
    note: str | None = None


@app.post("/api/applications")
def create_application(
    body: ApplicationCreate,
    user: dict | None = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    """创建投递记录（直达链接模式）。authorized=false 拒绝（§4 合规钩子）。

    §14 ②：授权必须留痕——记 `authorized_at` 与 `consent_version`。前端展示的政策版本与
    服务端当前版本不一致时按 400 拒绝（用户在旧政策页上同意的内容不能算作对新版同意），
    服务端调用未带版本时按当前版本补记（app/services/legal.py 口径）。
    """
    from app.services.legal import POLICY_VERSION, consent_version_ok

    if not body.authorized:
        raise HTTPException(status_code=403, detail="投递需用户授权（authorized=true）")
    if not consent_version_ok(body.consent_version):
        raise HTTPException(
            status_code=400,
            detail=f"政策版本不一致（收到 {body.consent_version}，当前 {POLICY_VERSION}），请刷新页面重新确认",
        )
    job = session.get(Job, body.job_id)
    if job is None or job.status != "active":
        raise HTTPException(status_code=404, detail="职位不存在或已失效")
    # 登录态下以令牌主体为准：body.user_id 改成别人也不成立（越权 403）
    owner_id = _scope_user_id(body.user_id, user)
    if body.resume_id is not None:
        _owned(session, Resume, body.resume_id, user, "简历")

    # 防重复：同用户同职位已有进行中的申请
    dup = session.execute(
        select(Application).where(
            Application.user_id == owner_id,
            Application.job_id == body.job_id,
            Application.status.notin_(["closed", "rejected"]),
        )
    ).scalars().first()
    if dup is not None:
        raise HTTPException(status_code=409, detail=f"该职位已在投递流程中（#{dup.id}，{dup.status}）")

    app_row = Application(
        user_id=owner_id,
        resume_id=body.resume_id,
        job_id=body.job_id,
        mode="direct_link",  # 半自动帮填（semi_auto）为 P3 后半
        status="submitted",
        apply_url=job.apply_url,
        authorized=True,  # §14 ② 走到这里必已 authorized=true，显式落库便于审计
        authorized_at=datetime.utcnow(),
        consent_version=body.consent_version or POLICY_VERSION,
    )
    session.add(app_row)
    session.commit()
    return {
        "code": 0,
        "data": {
            "id": app_row.id,
            "status": app_row.status,
            "apply_url": app_row.apply_url,
            "authorized_at": str(app_row.authorized_at),
            "consent_version": app_row.consent_version,
        },
        "message": "ok",
    }


@app.get("/api/applications")
def list_applications(
    user_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
    user: dict | None = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    user_id = _scope_user_id(user_id, user)
    stmt = select(Application, Job).join(Job, Application.job_id == Job.id)
    count_stmt = select(func.count()).select_from(Application)
    if user_id is not None:
        stmt = stmt.where(Application.user_id == user_id)
        count_stmt = count_stmt.where(Application.user_id == user_id)
    if status is not None:
        stmt = stmt.where(Application.status == status)
        count_stmt = count_stmt.where(Application.status == status)
    total = session.execute(count_stmt).scalar_one()
    rows = session.execute(stmt.order_by(Application.updated_at.desc()).limit(min(limit, 200))).all()
    return {
        "code": 0,
        "data": {
            "total": total,
            "items": [
                {
                    "id": app.id,
                    "user_id": app.user_id,
                    "job_id": job.id,
                    "job_title": job.title,
                    "city": job.city,
                    "status": app.status,
                    "apply_url": app.apply_url or job.apply_url,
                    # §14 ② 同意留痕：授权状态/时间/政策版本（历史行可能为 null，如实透出）
                    "authorized": bool(app.authorized),
                    "authorized_at": str(app.authorized_at) if app.authorized_at else None,
                    "consent_version": app.consent_version,
                    "created_at": str(app.created_at),
                    "updated_at": str(app.updated_at),
                }
                for app, job in rows
            ],
        },
        "message": "ok",
    }


@app.post("/api/applications/{application_id}/status")
def update_application_status(
    application_id: int,
    body: StatusUpdate,
    user: dict | None = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    """状态机迁移（非法迁移 400），变更写入 feedback_log。"""
    app_row = _owned(session, Application, application_id, user, "投递记录")
    try:
        app_row = transition(session, app_row, body.status, note=body.note)
    except IllegalTransition as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "code": 0,
        "data": {"id": app_row.id, "status": app_row.status},
        "message": "ok",
    }


@app.get("/api/reminders")
def reminders(
    user_id: int | None = None,
    user: dict | None = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    """催进扫描：submitted/under_review 卡超过 T 天（settings.reminder_after_days，默认 3）。"""
    user_id = _scope_user_id(user_id, user)
    items = scan_reminders(session, user_id=user_id)
    return {"code": 0, "data": {"total": len(items), "items": items}, "message": "ok"}


# ---------- P4 收尾：companies admin API（挂账销项） ----------


class CompanyUpsert(BaseModel):
    slug: str
    name: str
    ats_type: str
    site_url: str | None = None
    feed_url: str | None = None
    locale: str = "zh-CN"
    auth_type: str = "public"
    fetch_policy: dict = {"interval_min": 360}
    is_active: bool = True


def _company_out(session: Session, company: Company) -> dict:
    job_count = session.execute(
        select(func.count()).select_from(Job).where(Job.company_id == company.id)
    ).scalar_one()
    return {
        "id": company.id,
        "slug": company.slug,
        "name": company.name,
        "ats_type": company.ats_type,
        "site_url": company.site_url,
        "feed_url": company.feed_url,
        "locale": company.locale,
        "auth_type": company.auth_type,
        "fetch_policy": company.fetch_policy,
        "is_active": bool(company.is_active),
        "job_count": job_count,
    }


@app.get("/api/companies")
def list_companies(
    is_active: bool | None = None, session: Session = Depends(get_session)
) -> dict:
    stmt = select(Company).order_by(Company.id.asc())
    if is_active is not None:
        stmt = stmt.where(Company.is_active.is_(is_active))
    rows = session.execute(stmt).scalars().all()
    return {
        "code": 0,
        "data": {"total": len(rows), "items": [_company_out(session, c) for c in rows]},
        "message": "ok",
    }


@app.post("/api/companies")
def create_company(
    body: CompanyUpsert,
    user: dict | None = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    """新增公司映射；slug 冲突 409，ats_type 未注册 400（采集前先校验）。"""
    from app.adapters.registry import ADAPTERS

    _require_admin(user)
    if session.execute(
        select(func.count()).select_from(Company).where(Company.slug == body.slug)
    ).scalar_one():
        raise HTTPException(status_code=409, detail=f"slug 已存在: {body.slug}")
    if body.ats_type not in ADAPTERS:
        raise HTTPException(
            status_code=400,
            detail=f"未注册的 ATS 类型: {body.ats_type}（可用: {sorted(ADAPTERS)}）",
        )
    company = Company(**body.model_dump())
    session.add(company)
    session.commit()
    return {"code": 0, "data": _company_out(session, company), "message": "ok"}


@app.put("/api/companies/{company_id}")
def update_company(
    company_id: int,
    body: CompanyUpsert,
    user: dict | None = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    """全量更新公司映射（含 is_active 启停）。"""
    from app.adapters.registry import ADAPTERS

    _require_admin(user)
    company = session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="公司不存在")
    if body.ats_type not in ADAPTERS:
        raise HTTPException(
            status_code=400,
            detail=f"未注册的 ATS 类型: {body.ats_type}（可用: {sorted(ADAPTERS)}）",
        )
    dup = session.execute(
        select(Company).where(Company.slug == body.slug, Company.id != company_id)
    ).scalars().first()
    if dup is not None:
        raise HTTPException(status_code=409, detail=f"slug 已存在: {body.slug}")
    for key, value in body.model_dump().items():
        setattr(company, key, value)
    session.commit()
    return {"code": 0, "data": _company_out(session, company), "message": "ok"}


@app.delete("/api/companies/{company_id}")
def delete_company(
    company_id: int,
    force: bool = False,
    user: dict | None = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    """删除公司映射；有职位时默认 409 拒绝，force=true 连带职位与 match_scores。"""
    _require_admin(user)
    company = session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="公司不存在")
    job_ids = [
        row for row in session.execute(select(Job.id).where(Job.company_id == company.id)).scalars()
    ]
    if job_ids and not force:
        raise HTTPException(
            status_code=409,
            detail=f"该公司下有 {len(job_ids)} 条职位；确认删除请加 force=true（连带 match_scores）",
        )
    if job_ids:
        session.execute(delete(MatchScore).where(MatchScore.job_id.in_(job_ids)))
        session.execute(delete(Job).where(Job.company_id == company.id))
    session.delete(company)
    session.commit()
    return {
        "code": 0,
        "data": {"id": company_id, "deleted": True, "jobs_deleted": len(job_ids)},
        "message": "ok",
    }


# ---------- P4 收尾：质量基线报告（挂账销项） ----------


@app.get("/api/insights/quality")
def quality_insights(
    user_id: int | None = None,
    k: int = 10,
    session: Session = Depends(get_session),
) -> dict:
    """质量基线：反馈覆盖 / 命中率 / 各结果组均分 / NDCG@k（口径见 insights.py）。"""
    from app.services.insights import build_quality_report

    report = build_quality_report(session, user_id=user_id, k=k)
    return {"code": 0, "data": report, "message": "ok"}


@app.get("/api/insights/tuning")
def tuning_insights(
    k: int = 10,
    step: float = Query(0.1, ge=0.05, le=0.5),
    top: int = 5,
    session: Session = Depends(get_session),
) -> dict:
    """权重回归调参建议：在权重网格上搜索 NDCG@k 最优（只读，不写配置）。

    step 表示权重网格粒度（越小越细但组合数指数上升）；越界返回 422。
    """
    from app.services.tuning import search_weights

    report = search_weights(session, k=k, step=step, top=top)
    return {"code": 0, "data": report, "message": "ok"}


# ---------- §12.5 反馈回灌闭环：权重版本的采纳 / 回滚 / 自动调参 ----------


class WeightApplyRequest(BaseModel):
    weights: dict
    note: str | None = None
    rematch: bool = True  # 采纳后是否立即全量重算 match_scores


class AutoTuneRequest(BaseModel):
    k: int = 10
    step: float = 0.1
    top: int = 5
    min_sample: int | None = None
    margin: float | None = None
    apply: bool = False  # 默认只评估不生效（dry-run）
    rematch: bool = True


@app.get("/api/match-weights")
def list_match_weights(
    limit: int = Query(20, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict:
    """当前生效权重 + 采纳历史（历史即审计轨迹，可回滚）。"""
    from app.services.weights import active_weights, history

    active = active_weights(session)
    return {
        "code": 0,
        "data": {
            "active": active.as_dict(),
            "source": active.source,
            "history": history(session, limit=limit),
        },
        "message": "ok",
    }


@app.post("/api/match-weights/apply")
def apply_match_weights(
    body: WeightApplyRequest,
    user: dict | None = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    """采纳一组权重（管理动作）；权重不自洽 → 400，采纳后默认全量重算 match_scores。"""
    from app.services.weights import apply_weights

    _require_admin(user)
    try:
        data = apply_weights(
            session, body.weights, source="manual", note=body.note, rematch=body.rematch
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"code": 0, "data": data, "message": "ok"}


@app.post("/api/match-weights/rollback")
def rollback_match_weights(
    rematch: bool = True,
    user: dict | None = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    """回滚到上一版；没有采纳记录时 400（当前用的是 .env 设置权重）。"""
    from app.services.weights import rollback

    _require_admin(user)
    try:
        data = rollback(session, rematch=rematch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"code": 0, "data": data, "message": "ok"}


@app.post("/api/match-weights/auto-tune")
def auto_tune_match_weights(
    body: AutoTuneRequest,
    user: dict | None = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    """跑一次"反馈 → 调参 → （可选）采纳"：样本与增益双门槛不过就不换参数。"""
    from app.services.weights import auto_tune

    _require_admin(user)
    if not 0.05 <= body.step <= 0.5:
        raise HTTPException(status_code=422, detail="step 仅支持 [0.05, 0.5]")
    data = auto_tune(
        session,
        k=body.k,
        step=body.step,
        top=body.top,
        min_sample=body.min_sample,
        margin=body.margin,
        apply=body.apply,
        rematch=body.rematch,
    )
    return {"code": 0, "data": data, "message": "ok"}


# ---------- P5 ① 市场洞察报告（§12.6） ----------


@app.get("/api/insights/market")
def market_insights(
    region: str = "all",
    top: int = Query(10, ge=1, le=50),
    min_sample: int = Query(3, ge=1, le=100),
    months: int = Query(12, ge=1, le=36),
    session: Session = Depends(get_session),
) -> dict:
    """城市/技能/薪资趋势（脱敏聚合：分桶计数 < min_sample 不单独输出）。"""
    if region not in ("all", "cn", "overseas"):
        raise HTTPException(status_code=422, detail="region 仅支持 all|cn|overseas")
    from app.services.market import market_report

    report = market_report(session, region=region, top=top, min_sample=min_sample, months=months)
    return {"code": 0, "data": report, "message": "ok"}


# ---------- P5 ② 面试陪伴闭环（§12.6） ----------


class InterviewAtUpdate(BaseModel):
    interview_at: datetime | None = None


@app.get("/api/applications/{application_id}/interview-kit")
def interview_kit(
    application_id: int,
    user: dict | None = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    """面试陪伴包：准备清单 + 技能考察点 + 公司背景包（规则推导，离线可算）。"""
    app_row = _owned(session, Application, application_id, user, "投递记录")
    from app.services.interview import build_interview_kit

    return {"code": 0, "data": build_interview_kit(session, app_row), "message": "ok"}


@app.put("/api/applications/{application_id}/interview-at")
def update_interview_at(
    application_id: int,
    body: InterviewAtUpdate,
    user: dict | None = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    """登记/清空面试时间（终态 400）；登记后进入 beat 的面试催进窗口。"""
    app_row = _owned(session, Application, application_id, user, "投递记录")
    from app.services.interview import set_interview_at

    try:
        app_row = set_interview_at(session, app_row, body.interview_at)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "code": 0,
        "data": {
            "id": app_row.id,
            "status": app_row.status,
            "interview_at": str(app_row.interview_at) if app_row.interview_at else None,
        },
        "message": "ok",
    }


@app.get("/api/interview-reminders")
def interview_reminders(
    user_id: int | None = None,
    within_days: int = Query(2, ge=0, le=30),
    user: dict | None = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    """面试催进扫描（平台内查询式，与 beat 的 interview_reminder_task 同源）。"""
    from app.services.interview import scan_interview_reminders

    user_id = _scope_user_id(user_id, user)
    items = scan_interview_reminders(session, within_days=within_days, user_id=user_id)
    return {"code": 0, "data": {"total": len(items), "items": items}, "message": "ok"}


# ---------- P5 ③ AI 简历优化（§12.6） ----------


class OptimizeRequest(BaseModel):
    job_id: int
    use_llm: bool = False  # LLM 兜底默认关（规则为主；开启且未配 key 也只在 llm.status 标注）


@app.post("/api/resumes/{resume_id}/optimize")
def optimize_resume(
    resume_id: int,
    body: OptimizeRequest,
    user: dict | None = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    """对照目标 JD 逐条差距 + 改写建议（规则推导离线可算，LLM 为可选兜底）。"""
    resume = _owned(session, Resume, resume_id, user, "简历")
    job = session.get(Job, body.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="职位不存在")
    from app.services.optimize import build_optimization

    return {"code": 0, "data": build_optimization(resume, job, use_llm=body.use_llm), "message": "ok"}


# ---------- P5 ⑤ 招聘季适配 + 多语言看板（§12.6） ----------


@app.get("/api/insights/seasonality")
def seasonality_insights(
    region: str = "all",
    lang: str = "zh-CN",
    min_sample: int = Query(3, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict:
    """招聘季画像：月份/季度分布 + 当前冷热与建议（标签按 lang 渲染）。"""
    if region not in ("all", "cn", "overseas"):
        raise HTTPException(status_code=422, detail="region 仅支持 all|cn|overseas")
    from app.services.i18n import SUPPORTED_LANGS
    from app.services.seasonality import seasonality_report

    if lang not in SUPPORTED_LANGS:
        raise HTTPException(status_code=422, detail="lang 仅支持 zh-CN|en")
    report = seasonality_report(session, region=region, lang=lang, min_sample=min_sample)
    return {"code": 0, "data": report, "message": "ok"}


@app.post("/api/applications/{application_id}/autofill")
def autofill_application(
    application_id: int,
    user: dict | None = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    """半自动帮填：有头浏览器打开申请页并预填常见字段，由用户人工核对提交。

    只填公开表单（姓名/邮箱/电话），绝不自动提交、不碰登录墙/验证码（§4/§8）。
    """
    from app.services.autofill import AutofillNotConfigured, launch_autofill_thread

    app_row = _owned(session, Application, application_id, user, "投递记录")
    if app_row.status in ("closed", "rejected"):
        raise HTTPException(status_code=400, detail="该投递已终止，无需帮填")

    apply_url = app_row.apply_url
    if not apply_url:
        raise HTTPException(status_code=400, detail="申请链接缺失")
    try:
        launch_autofill_thread(apply_url)
    except AutofillNotConfigured as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "code": 0,
        "data": {"application_id": application_id, "status": "launched", "apply_url": apply_url},
        "message": "浏览器已打开，请人工核对后手动提交",
    }


# ---------- §14 上线合规：政策版本 + 投递告知（法务最小实现） ----------


@app.get("/api/legal/policies")
def legal_policies() -> dict:
    """政策版本/生效日 + 投递前告知文案（前端 `/legal/*` 与投递确认弹层的单一事实源）。

    政策正文在前端静态页；后端给版本与告知要点，供 `POST /api/applications` 的
    `consent_version` 留痕比对（口径见 app/services/legal.py）。
    """
    from app.services.legal import policies_payload

    return {"code": 0, "data": policies_payload(), "message": "ok"}


@app.get("/api/compliance/audit")
def compliance_audit(session: Session = Depends(get_session)) -> dict:
    """采集 robots/terms 合规审计表（逐公司结论 + 证据 + 复核日期 + 覆盖缺口）。

    审计行是代码内数据（`app/services/compliance.py` AUDIT_ROWS），接口只做
    "库内公司 × 审计表"的覆盖比对：**未有审计行的公司单列在 `unreviewed`**——新增公司
    必须先补审计行再开采集（§5.2 合规前置），这条靠接口/用例兜住而不是靠人记。
    """
    from app.services.compliance import audit_report

    return {"code": 0, "data": audit_report(session), "message": "ok"}


# ---------- §14 ④ 上线监控看板：采集成功率 / 任务积压 / 命中率基线 ----------


@app.get("/api/insights/ops")
def ops_insights(
    hours: int = Query(24, ge=1, le=720),
    k: int = Query(10, ge=1, le=50),
    session: Session = Depends(get_session),
) -> dict:
    """运维监控看板：采集成功率 / 任务积压 / 命中率基线 + 告警（口径见 services/ops.py）。

    只读聚合，不触网：Redis 不可用时队列口径降级为 available=false（不拿 0 冒充健康）。
    hours 越界返回 422。
    """
    from app.services.ops import ops_report

    return {"code": 0, "data": ops_report(session, hours=hours, k=k), "message": "ok"}
