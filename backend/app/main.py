"""FastAPI 入口（P1：健康检查 + 职位列表；P2：简历上传解析；鉴权/推荐等 P2+ 接入）。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import SessionLocal
from app.models import Application, Job, MatchScore, Resume
from app.pipelines.match import refresh_matches
from app.pipelines.parse import parse_resume_text
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
        conds = [
            func.lower(Job.city).like(f"%{v}%", escape="\\")
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

    try:
        profile = parse_resume_text(text, session)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    resume = Resume(
        user_id=user_id,
        file_path=saved_path,
        raw_text=text,
        profile=profile,
        lang=profile.get("lang", "zh"),
        is_active=True,  # 新上传自动成为该用户当前生效简历
    )
    # 同用户其他简历取消 active
    for other in session.execute(
        select(Resume).where(Resume.user_id == user_id, Resume.id != resume.id, Resume.is_active.is_(True))
    ).scalars():
        other.is_active = False
    session.add(resume)
    session.commit()
    return {
        "code": 0,
        "data": {"id": resume.id, "profile": profile, "source": profile.get("source"), "is_active": True},
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
    for other in session.execute(
        select(Resume).where(Resume.user_id == resume.user_id, Resume.id != resume.id)
    ).scalars():
        other.is_active = False
    resume.is_active = True
    session.commit()
    return {"code": 0, "data": {"id": resume.id, "is_active": True}, "message": "ok"}


@app.delete("/api/resumes/{resume_id}")
def delete_resume(resume_id: int, session: Session = Depends(get_session)) -> dict:
    """删除简历（连带 match_scores；上传原件文件保留在磁盘）。"""
    resume = session.get(Resume, resume_id)
    if resume is None:
        raise HTTPException(status_code=404, detail="简历不存在")
    session.execute(
        delete(MatchScore).where(MatchScore.resume_id == resume.id)
    )
    session.delete(resume)
    session.commit()
    return {"code": 0, "data": {"id": resume_id, "deleted": True}, "message": "ok"}


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
    resume.profile = current
    session.commit()
    # §12.3：profile 修改后触发重匹配（同步全量重算，规则打分轻量）
    refreshed = refresh_matches(session, resume)
    return {
        "code": 0,
        "data": {"id": resume.id, "profile": current, "matches_refreshed": refreshed},
        "message": "ok",
    }


# ---------- P2 推荐（§7） ----------


@app.get("/api/recommend")
def recommend(
    resume_id: int,
    limit: int = 20,
    offset: int = 0,
    refresh: bool = False,
    session: Session = Depends(get_session),
) -> dict:
    """可解释排序推荐：读 match_scores 缓存；无缓存或 refresh=true 时全量重算。"""
    resume = session.get(Resume, resume_id)
    if resume is None:
        raise HTTPException(status_code=404, detail="简历不存在")

    cached = session.execute(
        select(func.count()).select_from(MatchScore).where(MatchScore.resume_id == resume.id)
    ).scalar_one()
    if refresh or cached == 0:
        refresh_matches(session, resume)

    stmt = (
        select(MatchScore, Job)
        .join(Job, MatchScore.job_id == Job.id)
        .where(MatchScore.resume_id == resume.id, Job.status == "active")
        .order_by(MatchScore.final_score.desc(), MatchScore.rule_score.desc(), Job.updated_at.desc())
    )
    total = session.execute(
        select(func.count())
        .select_from(MatchScore)
        .join(Job, MatchScore.job_id == Job.id)
        .where(MatchScore.resume_id == resume.id, Job.status == "active")
    ).scalar_one()
    rows = session.execute(stmt.limit(min(limit, 200)).offset(offset)).all()
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


class StatusUpdate(BaseModel):
    status: str
    note: str | None = None


@app.post("/api/applications")
def create_application(
    body: ApplicationCreate, session: Session = Depends(get_session)
) -> dict:
    """创建投递记录（直达链接模式）。authorized=false 拒绝（§4 合规钩子）。"""
    if not body.authorized:
        raise HTTPException(status_code=403, detail="投递需用户授权（authorized=true）")
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
    )
    session.add(app_row)
    session.commit()
    return {
        "code": 0,
        "data": {
            "id": app_row.id,
            "status": app_row.status,
            "apply_url": app_row.apply_url,
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
