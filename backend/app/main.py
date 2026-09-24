"""FastAPI 入口（P1：健康检查 + 职位列表；P2：简历上传解析；鉴权/推荐等 P2+ 接入）。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import SessionLocal
from app.models import Application, Company, Job, MatchScore, Resume
from app.pipelines.match import refresh_matches
from app.pipelines.parse import parse_resume_text
from app.pipelines.rules import detect_lang
from app.pipelines.text_extract import UnsupportedFile, extract_text
from app.services.applications import IllegalTransition, scan_reminders, transition
from app.services.city import city_match_variants


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


app = FastAPI(title="Job Direct Apply", version="0.1.0")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


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
    session: Session = Depends(get_session),
) -> dict:
    """上传简历（txt/md/pdf 或直接贴文本），解析入库并返回 profile。"""
    saved_path: str | None = None
    try:
        if file is not None:
            data = await file.read()
            filename = file.filename or "resume.txt"
            text = extract_text(filename, data)
            # 原件存盘，便于追溯/重解析
            uploads = Path(settings.uploads_dir)
            uploads.mkdir(parents=True, exist_ok=True)
            suffix = "." + filename.rsplit(".", 1)[-1].lower()
            dest = uploads / f"resume_{user_id}_{int(datetime.now().timestamp())}{suffix}"
            dest.write_bytes(data)
            saved_path = str(dest)
        elif raw_text:
            text = raw_text
        else:
            raise HTTPException(status_code=400, detail="需要 file 或 raw_text 之一")
    except UnsupportedFile as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

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
        },
        "message": "ok",
    }


@app.get("/api/resumes")
def list_resumes(user_id: int | None = None, session: Session = Depends(get_session)) -> dict:
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
def activate_resume(resume_id: int, session: Session = Depends(get_session)) -> dict:
    """标记该简历为当前生效（同用户其他简历取消）。"""
    resume = session.get(Resume, resume_id)
    if resume is None:
        raise HTTPException(status_code=404, detail="简历不存在")
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
def delete_resume(resume_id: int, session: Session = Depends(get_session)) -> dict:
    """删除简历（连带 match_scores + 磁盘原件，§14 ① 个人信息删除口径）。"""
    resume = session.get(Resume, resume_id)
    if resume is None:
        raise HTTPException(status_code=404, detail="简历不存在")
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
def get_resume(resume_id: int, session: Session = Depends(get_session)) -> dict:
    resume = session.get(Resume, resume_id)
    if resume is None:
        raise HTTPException(status_code=404, detail="简历不存在")
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
    session: Session = Depends(get_session),
) -> dict:
    """人工修正 profile（白名单字段）；修改后触发重匹配在匹配引擎接入时生效。"""
    resume = session.get(Resume, resume_id)
    if resume is None:
        raise HTTPException(status_code=404, detail="简历不存在")

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


@app.get("/api/recommend")
def recommend(
    resume_id: int,
    limit: int = 20,
    offset: int = 0,
    refresh: bool = False,
    region: str = "all",  # all=全部 | cn=国内 | overseas=海外
    session: Session = Depends(get_session),
) -> dict:
    """可解释排序推荐：读 match_scores 缓存；无缓存或 refresh=true 时全量重算。"""
    if region not in ("all", "cn", "overseas"):
        raise HTTPException(status_code=422, detail="region 仅支持 all|cn|overseas")

    resume = session.get(Resume, resume_id)
    if resume is None:
        raise HTTPException(status_code=404, detail="简历不存在")

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

    stmt = (
        select(MatchScore, Job)
        .join(Job, MatchScore.job_id == Job.id)
        .where(*where_clauses)
        .order_by(MatchScore.final_score.desc(), MatchScore.rule_score.desc(), Job.updated_at.desc())
    )
    total = session.execute(
        select(func.count())
        .select_from(MatchScore)
        .join(Job, MatchScore.job_id == Job.id)
        .where(*where_clauses)
    ).scalar_one()
    window = min(limit, 200)
    if region == "all":
        # 方案 A：两路各取前 offset+window 条后交错，等价于全局交错排序的该窗口
        fetch_n = offset + window
        cn_rows = session.execute(
            stmt.where(Job.source.in_(DOMESTIC_SOURCES)).limit(fetch_n)
        ).all()
        overseas_rows = session.execute(
            stmt.where(or_(Job.source.is_(None), Job.source.notin_(DOMESTIC_SOURCES))).limit(fetch_n)
        ).all()
        rows = _mix_regions(cn_rows, overseas_rows)[offset : offset + window]
    else:
        rows = session.execute(stmt.limit(window).offset(offset)).all()
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
    body: ApplicationCreate, session: Session = Depends(get_session)
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
    if body.resume_id is not None and session.get(Resume, body.resume_id) is None:
        raise HTTPException(status_code=404, detail="简历不存在")

    # 防重复：同用户同职位已有进行中的申请
    dup = session.execute(
        select(Application).where(
            Application.user_id == body.user_id,
            Application.job_id == body.job_id,
            Application.status.notin_(["closed", "rejected"]),
        )
    ).scalars().first()
    if dup is not None:
        raise HTTPException(status_code=409, detail=f"该职位已在投递流程中（#{dup.id}，{dup.status}）")

    app_row = Application(
        user_id=body.user_id,
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
    session: Session = Depends(get_session),
) -> dict:
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
    application_id: int, body: StatusUpdate, session: Session = Depends(get_session)
) -> dict:
    """状态机迁移（非法迁移 400），变更写入 feedback_log。"""
    app_row = session.get(Application, application_id)
    if app_row is None:
        raise HTTPException(status_code=404, detail="投递记录不存在")
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
    user_id: int | None = None, session: Session = Depends(get_session)
) -> dict:
    """催进扫描：submitted/under_review 卡超过 T 天（settings.reminder_after_days，默认 3）。"""
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
def create_company(body: CompanyUpsert, session: Session = Depends(get_session)) -> dict:
    """新增公司映射；slug 冲突 409，ats_type 未注册 400（采集前先校验）。"""
    from app.adapters.registry import ADAPTERS

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
    company_id: int, body: CompanyUpsert, session: Session = Depends(get_session)
) -> dict:
    """全量更新公司映射（含 is_active 启停）。"""
    from app.adapters.registry import ADAPTERS

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
    company_id: int, force: bool = False, session: Session = Depends(get_session)
) -> dict:
    """删除公司映射；有职位时默认 409 拒绝，force=true 连带职位与 match_scores。"""
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
def interview_kit(application_id: int, session: Session = Depends(get_session)) -> dict:
    """面试陪伴包：准备清单 + 技能考察点 + 公司背景包（规则推导，离线可算）。"""
    app_row = session.get(Application, application_id)
    if app_row is None:
        raise HTTPException(status_code=404, detail="投递记录不存在")
    from app.services.interview import build_interview_kit

    return {"code": 0, "data": build_interview_kit(session, app_row), "message": "ok"}


@app.put("/api/applications/{application_id}/interview-at")
def update_interview_at(
    application_id: int, body: InterviewAtUpdate, session: Session = Depends(get_session)
) -> dict:
    """登记/清空面试时间（终态 400）；登记后进入 beat 的面试催进窗口。"""
    app_row = session.get(Application, application_id)
    if app_row is None:
        raise HTTPException(status_code=404, detail="投递记录不存在")
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
    session: Session = Depends(get_session),
) -> dict:
    """面试催进扫描（平台内查询式，与 beat 的 interview_reminder_task 同源）。"""
    from app.services.interview import scan_interview_reminders

    items = scan_interview_reminders(session, within_days=within_days, user_id=user_id)
    return {"code": 0, "data": {"total": len(items), "items": items}, "message": "ok"}


# ---------- P5 ③ AI 简历优化（§12.6） ----------


class OptimizeRequest(BaseModel):
    job_id: int
    use_llm: bool = False  # LLM 兜底默认关（规则为主；开启且未配 key 也只在 llm.status 标注）


@app.post("/api/resumes/{resume_id}/optimize")
def optimize_resume(
    resume_id: int, body: OptimizeRequest, session: Session = Depends(get_session)
) -> dict:
    """对照目标 JD 逐条差距 + 改写建议（规则推导离线可算，LLM 为可选兜底）。"""
    resume = session.get(Resume, resume_id)
    if resume is None:
        raise HTTPException(status_code=404, detail="简历不存在")
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
    application_id: int, session: Session = Depends(get_session)
) -> dict:
    """半自动帮填：有头浏览器打开申请页并预填常见字段，由用户人工核对提交。

    只填公开表单（姓名/邮箱/电话），绝不自动提交、不碰登录墙/验证码（§4/§8）。
    """
    from app.services.autofill import AutofillNotConfigured, launch_autofill_thread

    app_row = session.get(Application, application_id)
    if app_row is None:
        raise HTTPException(status_code=404, detail="投递记录不存在")
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
