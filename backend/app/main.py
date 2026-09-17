"""FastAPI 入口（P1：健康检查 + 职位列表；P2：简历上传解析；鉴权/推荐等 P2+ 接入）。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import SessionLocal
from app.models import Job, MatchScore, Resume
from app.pipelines.match import refresh_matches
from app.pipelines.parse import parse_resume_text
from app.pipelines.text_extract import UnsupportedFile, extract_text
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
    )
    session.add(resume)
    session.commit()
    return {
        "code": 0,
        "data": {"id": resume.id, "profile": profile, "source": profile.get("source")},
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
        .order_by(MatchScore.rule_score.desc(), Job.updated_at.desc())
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
                    "score": float(ms.rule_score) if ms.rule_score is not None else None,
                    "explain": ms.explain,
                }
                for ms, job in rows
            ],
        },
        "message": "ok",
    }
