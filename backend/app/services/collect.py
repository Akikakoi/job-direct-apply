"""采集编排：discover → normalize → 标签归一 → upsert → TTL 清理 → fetch_log 审计。

对应开发文档 §5.2 采集流程与 §9 幂等要求：
- 去重：按 UNIQUE (company_id, external_id) upsert；
- 限流：DB 间隔守卫（公司级最小采集间隔）+ Redis 令牌桶跨进程第二层
  （beat 与手动 CLI 并发时防双抓；Redis 不可用自动退化）；
- 保鲜：本轮"见过"的职位强制刷新 updated_at；连续 N 个周期未见 → expired；
- 审计：每次真实尝试写一条 fetch_log；
- 抽标签：入库后对本轮职位词典扫描补 skills（零成本），`job_tag_llm` 开时对剩余空标签职位走 LLM
  兜底（§12.7 #7，有 cap 上限、失败不阻塞入库）；
- 职位有增/改/过期或补出新标签 → 触发全量简历重算 match_scores（挂账销项，可关）。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.registry import get_adapter
from app.core.config import settings
from app.core.redis_utils import get_redis
from app.models import Company, FetchLog, Job, SkillTag
from app.services.city import city_keys
from app.services.ratelimit import acquire

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


ALIAS_MAP_CACHE_KEY = "cache:alias_map"


def build_alias_map(session: Session, use_cache: bool = True) -> dict[str, str]:
    """别名映射（BUILTIN + skill_tags 字典），Redis 可用时带 TTL 缓存（挂账销项）。

    缓存失效：TTL 到期（settings.alias_cache_ttl_s）或手动 DEL cache:alias_map。
    Redis 不可用时直查 DB，行为与旧版一致。
    """
    client = get_redis() if use_cache else None
    if client is not None:
        try:
            cached = client.get(ALIAS_MAP_CACHE_KEY)
            if cached:
                return json.loads(cached)
        except Exception:
            pass  # 缓存读失败按 miss 处理

    mapping = dict(BUILTIN_ALIAS)
    for tag in session.execute(select(SkillTag)).scalars():
        mapping[tag.canonical.lower()] = tag.canonical
        for alias in tag.aliases or []:
            mapping[str(alias).lower()] = tag.canonical

    if client is not None:
        try:
            client.setex(ALIAS_MAP_CACHE_KEY, settings.alias_cache_ttl_s, json.dumps(mapping, ensure_ascii=False))
        except Exception:
            pass  # 缓存写失败不影响主流程
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


def cleanup_idle_jobs(
    session: Session,
    now: datetime | None = None,
    ttl_days: int | None = None,
    rematch: bool = True,
) -> dict:
    """全局 TTL 下架（§9 idle_jobs_cleanup，每日 beat 兜底）。

    与 collect_company 内的 cleanup_stale 分工不同：后者是采集轮次内的公司级 TTL
    （interval_min × ttl_multiplier，分钟级），只覆盖仍在采集的公司；本函数按自然日
    兜底，覆盖**停用/删除公司留下的孤儿职位**——它们的职位不再被任何采集轮次访问，
    否则会永久停留在 active 并被推荐。
    幂等：只对 active 生效，重复执行第二次为 0。
    """
    now = now or datetime.utcnow()
    ttl_days = settings.idle_jobs_ttl_days if ttl_days is None else ttl_days
    threshold = now - timedelta(days=ttl_days)
    stale = (
        session.execute(
            select(Job).where(Job.status == "active", Job.updated_at < threshold)
        )
        .scalars()
        .all()
    )
    for job in stale:
        job.status = "expired"
    session.commit()

    result: dict = {"expired": len(stale), "ttl_days": ttl_days}
    # 与采集侧一致：职位下架后重算 match_scores，避免推荐窗口里残留 inactive 职位
    if rematch and stale:
        from app.pipelines.match import refresh_matches_all  # 延迟导入避免循环依赖

        result["rematched"] = refresh_matches_all(session)
    return result


def collect_company(
    session: Session,
    company: Company,
    now: datetime | None = None,
    client: httpx.Client | None = None,
    rematch: bool = True,
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

    # Redis 令牌桶第二层（跨进程防双抓）；None = Redis 不可用，沿用 DB 守卫
    allowed = acquire(company.slug, 1, _interval_min(company) * 60)
    if allowed is False:
        log = FetchLog(
            company_id=company.id, status="rate_limited", job_count=0, started_at=started
        )
        log.finished_at = datetime.utcnow()
        session.add(log)
        session.commit()
        return {"company": company.slug, "status": "skipped", "reason": "token_bucket"}

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
    round_jobs: list[Job] = []  # 本轮涉及的职位对象（抽标签用，按 external_id 去重）
    seen_eids: set[str] = set()
    for n in normalized:
        n.skills = canonicalize_skills(n.skills, alias_map)
        row = n.to_row()
        eid = row["external_id"]
        row["city_keys"] = city_keys(row.get("city"))  # 规范化多城市索引串（多值召回）
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
            # 出现在本轮结果里即为在架：复活此前被 TTL 误杀的职位。
            # 否则一旦因跳过/漏采超过 TTL 被置 expired，即使仍在源里也永远回不来
            # （TTL 的语义是"源里已消失"，而不是"曾经过期"）。
            job.status = "active"
            if eid in fresh_ids:
                pending[eid] = job  # 新插入行的批内重复：仅覆盖字段，不再计数
            else:
                # 强制保鲜：即使字段无变化也刷新 updated_at（否则会被 TTL 误杀）
                job.updated_at = datetime.utcnow()
                updated_ids.add(eid)
        if eid not in seen_eids:  # 批内重复只留一个对象，避免重复抽标签
            seen_eids.add(eid)
            round_jobs.append(job)

    # autoflush=False：必须先 flush 把本轮刷新的 updated_at 落库，否则
    # cleanup_stale 的 SELECT 读到的还是旧值，会把**本轮刚刷新过的职位**整批
    # 误判过期（采集间隔较长后重采即触发：该公司的职位会全部变 expired）。
    session.flush()
    expired = cleanup_stale(session, company, now)

    log.job_count = len(normalized)
    log.finished_at = datetime.utcnow()
    session.add(log)
    session.commit()

    result = {
        "company": company.slug,
        "status": "ok",
        "discovered": len(normalized),
        "inserted": inserted,
        "updated": len(updated_ids),
        "expired": expired,
    }
    # §12.7 #7 入库抽标签：词典优先（零成本）+ LLM 兜底（job_tag_llm 开关 / cap 上限）。
    # 放在 commit 之后：职位与 fetch_log 已落库，LLM 慢或挂都不会影响本轮入库结果。
    tagged = _tag_round_jobs(session, round_jobs, alias_map)
    result["tagged"] = tagged
    # 挂账销项：职位有增/改/过期，或本轮补出了新标签 → 全量简历重算 match_scores
    tagged_count = tagged.get("dict", 0) + tagged.get("llm", 0)
    if rematch and (inserted or updated_ids or expired or tagged_count):
        from app.pipelines.match import refresh_matches_all  # 延迟导入避免循环依赖

        result["rematched"] = refresh_matches_all(session)
    return result


def _tag_round_jobs(session: Session, round_jobs: list[Job], alias_map: dict[str, str]) -> dict:
    """入库抽标签的容错外壳：任何异常都不影响已提交的采集结果。"""
    if not round_jobs:
        return {"dict": 0, "llm": 0, "llm_failed": 0, "llm_skipped": 0}
    from app.pipelines.job_tags import tag_jobs  # 延迟导入：job_tags 反向依赖 canonicalize_skills

    try:
        stats = tag_jobs(
            session,
            round_jobs,
            alias_map,
            use_llm=settings.job_tag_llm,
            cap=settings.job_tag_llm_cap,
        )
        session.commit()
        return stats
    except Exception as exc:
        session.rollback()
        return {"error": f"{type(exc).__name__}: {exc}"}


def collect_all(session: Session, now: datetime | None = None) -> list[dict]:
    companies = session.execute(select(Company).where(Company.is_active.is_(True))).scalars().all()
    return [collect_company(session, c, now=now) for c in companies]
