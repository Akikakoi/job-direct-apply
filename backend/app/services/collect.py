"""采集编排：discover → normalize → 标签归一 → upsert → TTL 清理 → fetch_log 审计。

对应开发文档 §5.2 采集流程与 §9 幂等要求：
- 去重：按 UNIQUE (company_id, external_id) upsert；
- 限流：公司级最小采集间隔（fetch_policy.interval_min），间隔内直接 skip；
- 保鲜：本轮"见过"的职位强制刷新 updated_at；连续 N 个周期未见 → expired；
- 审计：每次真实尝试写一条 fetch_log。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.registry import get_adapter
from app.core.config import settings
from app.models import Company, FetchLog, Job, SkillTag

# 内置别名表：与 skill_tags 字典合并使用（§3.9）
BUILTIN_ALIAS: dict[str, str] = {
    # 内置字典自洽（无 skill_tags 表也能扫）：canonical 全名自映射
    "kubernetes": "kubernetes",
    "go": "go",
    "javascript": "javascript",
    "python": "python",
    "java": "java",
    "docker": "docker",
    "react": "react",
    "sql": "sql",
    "aws": "aws",
    "machine learning": "machine learning",
    "backend": "backend",
    # 常用别名
    "k8s": "kubernetes",
    "kube": "kubernetes",
    "golang": "go",
    "js": "javascript",
    "nodejs": "javascript",
    "node.js": "javascript",
    "py": "python",
    "ml": "machine learning",
    # "ai" 不映射：网易游戏文案里 "AI" 多指 NPC/游戏 AI，≠ 机器学习（实测 345 条误报）
    "人工智能": "machine learning",
    "机器学习": "machine learning",
    "后端": "backend",
    "spring boot": "spring boot",
    "mysql": "mysql",
    "redis": "redis",
}


def build_alias_map(session: Session) -> dict[str, str]:
    mapping = dict(BUILTIN_ALIAS)
    for tag in session.execute(select(SkillTag)).scalars():
        mapping[tag.canonical.lower()] = tag.canonical
        for alias in tag.aliases or []:
            mapping[str(alias).lower()] = tag.canonical
    return mapping


def canonicalize_skills(skills: list[str], alias_map: dict[str, str]) -> list[str]:
    """归一到标准标签；未命中字典的词小写原样保留（标记后续补录字典）。"""
    result: set[str] = set()
    for s in skills:
        if not s or not s.strip():
            continue
        lowered = s.strip().lower()
        result.add(alias_map.get(lowered, lowered))
    return sorted(result)


def _interval_min(company: Company) -> int:
    policy = company.fetch_policy or {}
    return int(policy.get("interval_min", settings.fetch_default_interval_min))


def cleanup_stale(session: Session, company: Company, now: datetime) -> int:
    """TTL 下架：active 且 updated_at 早于 threshold → expired。"""
    threshold = now - timedelta(minutes=_interval_min(company) * settings.ttl_multiplier)
    stale = (
        session.execute(
            select(Job).where(
                Job.company_id == company.id,
                Job.status == "active",
                Job.updated_at < threshold,
            )
        )
        .scalars()
        .all()
    )
    for job in stale:
        job.status = "expired"
    return len(stale)


def collect_company(
    session: Session,
    company: Company,
    now: datetime | None = None,
    client: httpx.Client | None = None,
) -> dict:
    now = now or datetime.utcnow()
    started = now

    last_ok = (
        session.execute(
            select(FetchLog)
            .where(FetchLog.company_id == company.id, FetchLog.status == "success")
            .order_by(FetchLog.started_at.desc())
        )
        .scalars()
        .first()
    )
    if last_ok is not None and last_ok.started_at >= now - timedelta(minutes=_interval_min(company)):
        return {"company": company.slug, "status": "skipped", "reason": "interval_guard"}

    log = FetchLog(company_id=company.id, status="success", job_count=0, started_at=started)
    try:
        adapter = get_adapter(company.ats_type, client=client)
        normalized = adapter.normalize_all(company)
    except Exception as exc:  # 含 NotImplementedError（official_site 骨架）
        log.status = "failed"
        log.error_msg = f"{type(exc).__name__}: {exc}"
        log.finished_at = datetime.utcnow()
        session.add(log)
        session.commit()
        return {"company": company.slug, "status": "failed", "error": str(exc)}

    alias_map = build_alias_map(session)
    inserted = 0
    pending: dict[str, Job] = {}  # 批内去重：autoflush=False 时 select 看不到未落库的新行
    fresh_ids: set[str] = set()  # 本轮新插入的 external_id（批内重复不再重复计数）
    updated_ids: set[str] = set()  # 本轮刷新的既有职位（按职位去重计数）
    for n in normalized:
        n.skills = canonicalize_skills(n.skills, alias_map)
        row = n.to_row()
        eid = row["external_id"]
        job = pending.get(eid)
        if job is None:
            job = (
                session.execute(
                    select(Job).where(Job.company_id == company.id, Job.external_id == eid)
                )
                .scalars()
                .first()
            )
        if job is None:
            job = Job(company_id=company.id, status="active", **row)
            session.add(job)
            pending[eid] = job
            fresh_ids.add(eid)
            inserted += 1
        else:
            for key, value in row.items():
                setattr(job, key, value)
            if eid in fresh_ids:
                pending[eid] = job  # 新插入行的批内重复：仅覆盖字段，不再计数
            else:
                # 强制保鲜：即使字段无变化也刷新 updated_at（否则会被 TTL 误杀）
                job.updated_at = datetime.utcnow()
                updated_ids.add(eid)

    expired = cleanup_stale(session, company, now)

    log.job_count = len(normalized)
    log.finished_at = datetime.utcnow()
    session.add(log)
    session.commit()
    return {
        "company": company.slug,
        "status": "ok",
        "discovered": len(normalized),
        "inserted": inserted,
        "updated": len(updated_ids),
        "expired": expired,
    }


def collect_all(session: Session, now: datetime | None = None) -> list[dict]:
    companies = session.execute(select(Company).where(Company.is_active.is_(True))).scalars().all()
    return [collect_company(session, c, now=now) for c in companies]
