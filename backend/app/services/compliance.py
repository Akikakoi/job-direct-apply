"""采集合规审计（§14 ③）：逐公司的 robots/terms 结论 + 证据 + 复核日期。

为什么是"数据表 + 覆盖比对"而不只是文档里的一段话：合规结论会随站点改版失效
（robots 改规则、接口加密、条款变更），必须**逐公司**记录"结论 + 证据 + 复核日期"，
而且能与库内公司清单做覆盖比对——新增公司没有审计行 = 漏审计，要能自动发现
（`tests/test_compliance.py` 对 seed 名单做全员断言，接口/脚本对库内实际行做比对）。

证据全部来自本仓库已有实测记录（§5.3 评估实录、各适配器 docstring、`fetch_log` 口径），
不新造结论；`decision` 只表达当前动作：collect（在采）/ dormant（休眠，保留评估证据）。

口径（枚举取值固定，避免自由文本漂移）：
- robots：allow（robots 明确允许）/ no_robots_fallback（无有效 robots，按惯例视为允许但仍低频）
  / not_applicable（不必走页面抓取，只用公开 JSON 接口）/ halted（因合规原因停止抓取）；
- terms：public_api_ok（使用官方公开接口且无需鉴权、不加密）/ terms_silent（未见明确条款，
  按"公开接口 + 低频 + 可追溯"保守处理）/ halted（条款或反爬层面不可用）。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Company

ROBOTS_CONCLUSIONS = ("allow", "no_robots_fallback", "not_applicable", "halted")
TERMS_CONCLUSIONS = ("public_api_ok", "terms_silent", "halted")
DECISIONS = ("collect", "dormant")

# 逐公司审计行（slug → 结论 + 证据 + 复核日期）。复核日期即该结论最后一次实测/复核的日期。
AUDIT_ROWS: list[dict] = [
    {
        "slug": "nvidia",
        "robots": "allow",
        "robots_note": "jobs.nvidia.com 允许 /careers 路径；实际只调 Workday 公开 cxs 接口，不抓页面",
        "terms": "public_api_ok",
        "terms_note": "Workday 外部招聘站点公开搜索接口（POST /wday/cxs/.../jobs），免鉴权",
        "decision": "collect",
        "decision_note": "间隔 360 分钟，最多 2000 条/轮",
        "reviewed_at": "2026-09-16",
    },
    {
        "slug": "stripe",
        "robots": "not_applicable",
        "robots_note": "只用 boards-api.greenhouse.io 公开 JSON，不抓 stripe.com 页面",
        "terms": "public_api_ok",
        "terms_note": "Greenhouse 公开 job board API（带 content=true 取全文），免鉴权",
        "decision": "collect",
        "decision_note": "间隔 360 分钟",
        "reviewed_at": "2026-09-16",
    },
    {
        "slug": "robinhood",
        "robots": "not_applicable",
        "robots_note": "同上，只用 Greenhouse 公开 boards API",
        "terms": "public_api_ok",
        "terms_note": "Greenhouse 公开 job board API，免鉴权",
        "decision": "collect",
        "decision_note": "间隔 360 分钟（第三家海外代表，替代已迁移的 Lever 站点）",
        "reviewed_at": "2026-09-16",
    },
    {
        "slug": "ashby",
        "robots": "not_applicable",
        "robots_note": "只用 jobs.ashbyhq.com posting-api，不抓页面",
        "terms": "public_api_ok",
        "terms_note": "Ashby posting-api 免鉴权单页全量，列表即带 descriptionPlain（零 N+1）",
        "decision": "collect",
        "decision_note": "间隔 360 分钟",
        "reviewed_at": "2026-09-18",
    },
    {
        "slug": "elevenlabs",
        "robots": "not_applicable",
        "robots_note": "同 Ashby 公开 posting-api",
        "terms": "public_api_ok",
        "terms_note": "Ashby posting-api 免鉴权",
        "decision": "collect",
        "decision_note": "间隔 360 分钟",
        "reviewed_at": "2026-09-18",
    },
    {
        "slug": "linear",
        "robots": "not_applicable",
        "robots_note": "同 Ashby 公开 posting-api",
        "terms": "public_api_ok",
        "terms_note": "Ashby posting-api 免鉴权",
        "decision": "collect",
        "decision_note": "间隔 360 分钟",
        "reviewed_at": "2026-09-18",
    },
    {
        "slug": "equinox",
        "robots": "not_applicable",
        "robots_note": "只用 SmartRecruiters 公开 API v1，不抓页面",
        "terms": "public_api_ok",
        "terms_note": "SmartRecruiters 公开 API v1（列表无 description，详情 N+1 默认关）",
        "decision": "collect",
        "decision_note": "间隔 360 分钟",
        "reviewed_at": "2026-09-18",
    },
    {
        "slug": "netease",
        "robots": "no_robots_fallback",
        "robots_note": "hr.163.com/robots.txt 返回 HTML 回退页（无真实 robots）→ 按惯例视为允许，但低频 720 分钟/轮",
        "terms": "public_api_ok",
        "terms_note": "hr.163.com 公开 JSON 接口免鉴权、响应不加密",
        "decision": "collect",
        "decision_note": "国内唯一在采源（M1 代表）；间隔 720 分钟",
        "reviewed_at": "2026-09-16",
    },
    {
        "slug": "dji",
        "robots": "halted",
        "robots_note": "we.dji.com 仅为品牌门面（无 SSR 职位数据、无 sitemap），未继续评估抓取",
        "terms": "halted",
        "terms_note": "实际走 Moka（apply.careers.dji.com），jobs/v2 响应整体加密；复刻前端混淆解密属**主动绕过反爬**（§5.2 禁止）",
        "decision": "dormant",
        "decision_note": "行保留但 is_active=false，评估证据留存；如国内需第二家，改用腾讯/米哈游等候选另行评估",
        "reviewed_at": "2026-09-16",
    },
    {
        # 以下两家是 P1 探测期写入库内的 Lever 板（未进 seed 名单）：站点已迁出 Lever、
        # 无有效职位，故给 dormant 审计行——避免报告里出现"未审计"的假缺口。
        "slug": "twitch",
        "robots": "not_applicable",
        "robots_note": "只用 Lever 公开 postings API，不抓页面",
        "terms": "public_api_ok",
        "terms_note": "Lever 公开 postings API（2026-09-16 实测板已迁移）",
        "decision": "dormant",
        "decision_note": "核实已迁出 Lever，无有效职位；不采集，评估结论留存",
        "reviewed_at": "2026-09-16",
    },
    {
        "slug": "anyscale",
        "robots": "not_applicable",
        "robots_note": "只用 Lever 公开 postings API，不抓页面",
        "terms": "public_api_ok",
        "terms_note": "Lever 公开 postings API（板内仅剩 1 条搬家公告）",
        "decision": "dormant",
        "decision_note": "同上：Lever 适配器保持就绪但无 M1 代表公司",
        "reviewed_at": "2026-09-16",
    },
]

AUDIT_BY_SLUG: dict[str, dict] = {row["slug"]: row for row in AUDIT_ROWS}


def validate_audit_rows(rows: list[dict] | None = None) -> list[str]:
    """结构自检：返回问题清单（空 = 通过）。供测试与脚本共用，避免枚举值自由漂移。"""
    rows = AUDIT_ROWS if rows is None else rows
    problems: list[str] = []
    seen: set[str] = set()
    for row in rows:
        slug = row.get("slug") or "<missing-slug>"
        if slug in seen:
            problems.append(f"{slug}: 审计行重复")
        seen.add(slug)
        for field in ("robots", "robots_note", "terms", "terms_note", "decision", "decision_note", "reviewed_at"):
            if not row.get(field):
                problems.append(f"{slug}: 缺少 {field}")
        if row.get("robots") not in ROBOTS_CONCLUSIONS:
            problems.append(f"{slug}: robots 取值非法 {row.get('robots')!r}")
        if row.get("terms") not in TERMS_CONCLUSIONS:
            problems.append(f"{slug}: terms 取值非法 {row.get('terms')!r}")
        if row.get("decision") not in DECISIONS:
            problems.append(f"{slug}: decision 取值非法 {row.get('decision')!r}")
    return problems


def audit_report(session: Session) -> dict:
    """库内公司 × 审计表：逐行给结论与证据，并单列**未审计**公司与休眠公司。"""
    rows: list[dict] = []
    for company in session.execute(select(Company).order_by(Company.id.asc())).scalars():
        audit = AUDIT_BY_SLUG.get(company.slug)
        item = {
            "slug": company.slug,
            "name": company.name,
            "ats_type": company.ats_type,
            "is_active": bool(company.is_active),
            "audited": audit is not None,
        }
        if audit is not None:
            item.update(audit)
        rows.append(item)

    missing = [r["slug"] for r in rows if not r["audited"]]
    dormant = [r["slug"] for r in rows if r.get("decision") == "dormant"]
    collecting = [r["slug"] for r in rows if r.get("decision") == "collect" and r["is_active"]]
    total = len(rows)
    return {
        "coverage": {
            "companies": total,
            "audited": total - len(missing),
            "ratio": round((total - len(missing)) / total, 4) if total else 0.0,
            "missing_slugs": missing,
        },
        "collecting": collecting,
        "dormant": dormant,
        # 未审计公司按 slug 单列：新增公司必须先补审计行再开采集（§5.2 合规前置）
        "unreviewed": missing,
        "rows": rows,
        "notes": [
            "审计证据来自 §5.3 评估实录与各适配器 docstring；复核日期为该结论最后一次实测/复核日。",
            "未有审计行的公司在 `unreviewed` 中单列——新增公司先补审计行再开采集。",
            "休眠公司（如 dji）保留在表中并记录原因，不做反爬绕过。",
        ],
    }


def markdown_table(rows: list[dict]) -> str:
    """渲染 Markdown 表（供 scripts/audit_compliance.py 直接贴进开发文档 §14）。"""
    header = "| 公司 | ATS | robots | terms | 结论 | 复核日 |"
    sep = "|---|---|---|---|---|---|"
    lines = [header, sep]
    for r in rows:
        label = r.get("name") or r["slug"]  # 纯代码内审计行（无 DB name）退化为 slug
        lines.append(
            f"| {label}（{r['slug']}） | {r.get('ats_type', '—')} | {r.get('robots', '—')} | "
            f"{r.get('terms', '—')} | {r.get('decision', '—')} | {r.get('reviewed_at', '—')} |"
        )
    return "\n".join(lines)