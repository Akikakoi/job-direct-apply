"""招聘季适配（§12.6 P5 ⑤）：按月份/季度看职位供给的旺淡季，并回答"现在该不该猛投"。

**为什么这么做**
- 求职者的第一个决策是"什么时候投"。"金三银四 / 金九银十"是行业口头共识，但本平台库里有
  真实的 `publish_date`，按自己的公司集合算出来的节奏比口头经验更贴合用户看到的职位列表。
- 结论必须诚实：只用 `publish_date`（真实发布日），**绝不回退 `created_at`**——采集时间会
  聚集到采集当月，跨年叠加后会把"我们什么时候采的"说成"市场什么时候招的"。

**口径说明**
- 统计范围：`jobs.status = active`；region（`all|cn|overseas`）复用 `market.region_of`，与 §4.2 同源。
- 月份分布：把多年数据投影到 1~12 月**叠加**（抹平单年噪声），零值月份保留，前端可直接画柱状图。
- 旺季/淡季判定：以"月均计数"为基准，`≥ 月均 × PEAK_RATIO` 记 `peak`，`≤ 月均 × OFF_RATIO`
  记 `off`，其余 `shoulder`；两个阈值挂模块常量，可读可调。
- 样本下限：有发布日期的职位 < `max(min_sample, MIN_DATED_JOBS)` 时**不给季节结论**——月均不足
  1 条时给任何月份贴旺季/淡季标签都是噪声。此时只回分布 + 说明，`season` 全为 `null`。
- 年份跨度：`years_covered` 一并返回；不足 2 年时在 `notes` 里明说"信号仅供参考"。

**取舍记录**
- 不引入外部招聘季日历（没有稳定的公开数据源，且行业差异极大）；
- 不做预测外推（几百条样本上的时间序列外推没有意义，只会产出看似精确的错误结论）；
- 报告只回结构化枚举，标签由 `services/i18n.py` 按 `lang` 渲染（全球多语言看板）。

**本项已收敛的边界（离线可测部分）**：季节画像 + 当前定位 + 中英标签（本模块 +
`GET /api/insights/seasonality`）。**剩余条件（非代码）**：行业细分季节（需行业标签）、
旺季到点订阅提醒（推送渠道与订阅名单属运营配置，可复用 `notify.py`）。
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job
from app.services.i18n import (
    DEFAULT_LANG,
    labels,
    month_label,
    quarter_label,
    resolve_lang,
)
from app.services.market import region_of

# 旺季/淡季判定阈值（相对"月均计数"的倍率）
PEAK_RATIO = 1.2
OFF_RATIO = 0.8
# 判定季节所需的最少"有发布日期"职位数：一年 12 个月，至少每月 1 条才有月均意义
MIN_DATED_JOBS = 12

_BASE_NOTES: dict[str, tuple[str, ...]] = {
    "zh-CN": (
        "统计口径：季节只认 publish_date（真实发布日），**不回退 created_at**——采集时间会聚集到采集当月，跨年叠加会整体失真。",
        "月份分布把多年数据投影到 1~12 月叠加；旺季 = 月计数 ≥ 月均 × {peak_ratio}，淡季 = ≤ 月均 × {off_ratio}，其余为平季。",
        "行业差异很大：这里反映的是**本平台已覆盖公司集合**的发布节奏，不等于全市场口径。",
    ),
    "en": (
        "Basis: season uses publish_date (real posting date) only, never falling back to created_at — crawl time clusters into the crawl month and would distort the cross-year picture.",
        "Month distribution folds multiple years into Jan..Dec; peak = count >= monthly avg x {peak_ratio}, off-season = count <= monthly avg x {off_ratio}, the rest is shoulder.",
        "Industry variance is large: this reflects the posting rhythm of the companies covered by this platform, not the whole market.",
    ),
}

_THIN_NOTES: dict[str, str] = {
    "zh-CN": "有发布日期的职位仅 {dated} 条（< {min_dated}），不足以判定旺季/淡季，本报告只回分布。",
    "en": "Only {dated} job(s) carry a publish date (< {min_dated}); too few to judge peak/off-season, so only the distribution is reported.",
}

_YEARS_NOTES: dict[str, str] = {
    "zh-CN": "样本只覆盖 {years} 个年份，季节信号仅供参考（覆盖 ≥ 2 个完整年份时更可靠）。",
    "en": "The sample covers only {years} year(s), so the season signal is indicative only (2+ full years is more reliable).",
}

_ADVICE: dict[str, dict[str, str]] = {
    "zh-CN": {
        "peak": "当前处于招聘旺季：新增职位最多，但 HR 收到的简历也最多——优先投匹配度最高的岗位，简历保持近期更新。",
        "shoulder": "当前处于招聘平季：职位供给平稳，适合定向投递 + 打磨简历与面试准备。",
        "off": "当前处于招聘淡季：新增职位少，但竞争者同样少；适合补技能、复盘投递反馈，在旺季前完成简历迭代。",
        "unknown": "有发布日期的样本不足，暂不给出季节建议；按常态节奏投递即可。",
    },
    "en": {
        "peak": "Peak hiring season: the most new postings, but also the most competition — prioritise the best-matching roles and keep your resume freshly updated.",
        "shoulder": "Shoulder season: supply is steady — good time for targeted applications, resume polish and interview prep.",
        "off": "Off-season: fewer new postings, but far fewer competitors — good time to build skills and review feedback, and to finish your resume rewrite before the peak.",
        "unknown": "Not enough dated postings to judge the season; just apply at your normal pace.",
    },
}


def _season_of(count: int, monthly_avg: float) -> str:
    """单个月份的相对冷热：peak / shoulder / off（以月均计数为基准）。"""
    if monthly_avg <= 0:
        return "shoulder"
    if count >= monthly_avg * PEAK_RATIO:
        return "peak"
    if count <= monthly_avg * OFF_RATIO:
        return "off"
    return "shoulder"


def _advice(lang: str, season: str | None) -> str:
    """当前季节对应的行动建议（`season=None` 即样本不足）。"""
    table = _ADVICE[resolve_lang(lang)]
    return table[season] if season else table["unknown"]


def seasonality_report(
    session: Session,
    region: str = "all",
    lang: str = DEFAULT_LANG,
    min_sample: int = 3,
    now: date | None = None,
) -> dict:
    """招聘季画像；region=all|cn|overseas（非法值由 API 层拦），lang 非法值在服务层回退。"""
    now = now or datetime.utcnow().date()
    key = resolve_lang(lang)
    lab = labels(key)

    jobs = session.execute(select(Job).where(Job.status == "active")).scalars().all()
    if region in ("cn", "overseas"):
        jobs = [j for j in jobs if region_of(j) == region]

    month_counter: Counter = Counter()
    quarter_counter: Counter = Counter()
    years: set[int] = set()
    dated = 0
    for job in jobs:
        published = job.publish_date  # 只认真实发布日，不回退 created_at（见模块 docstring）
        if published is None:
            continue
        dated += 1
        years.add(published.year)
        month_counter[published.month] += 1
        quarter_counter[(published.month - 1) // 3 + 1] += 1

    min_dated = max(min_sample, MIN_DATED_JOBS)
    enough = dated >= min_dated
    monthly_avg = round(dated / 12, 2) if dated else 0.0

    by_month = [
        {
            "month": m,
            "month_label": month_label(key, m),
            "count": month_counter.get(m, 0),
            "share": round(month_counter.get(m, 0) / dated, 4) if dated else None,
            "season": _season_of(month_counter.get(m, 0), monthly_avg) if enough else None,
        }
        for m in range(1, 13)
    ]

    by_quarter = [
        {
            "quarter": q,
            "quarter_label": quarter_label(key, q),
            "count": quarter_counter.get(q, 0),
            "share": round(quarter_counter.get(q, 0) / dated, 4) if dated else None,
        }
        for q in range(1, 5)
    ]

    current_month = now.month
    current_row = by_month[current_month - 1]
    current_quarter = (current_month - 1) // 3 + 1
    season = current_row["season"]
    current = {
        "month": current_month,
        "month_label": month_label(key, current_month),
        "quarter": current_quarter,
        "quarter_label": quarter_label(key, current_quarter),
        "month_count": current_row["count"],
        "season": season,
        "season_label": lab["season"][season] if season else None,
        "advice": _advice(key, season),
    }

    notes = [
        tpl.format(peak_ratio=PEAK_RATIO, off_ratio=OFF_RATIO) for tpl in _BASE_NOTES[key]
    ]
    if len(years) < 2:
        notes.append(_YEARS_NOTES[key].format(years=len(years)))
    if not enough:
        notes.append(_THIN_NOTES[key].format(dated=dated, min_dated=min_dated))

    return {
        "lang": key,
        "labels": lab,
        "scope": {
            "region": region,
            "region_label": lab["region"].get(region, region),
            "total_jobs": len(jobs),
            "with_publish_date": dated,
            "unknown_publish_date": len(jobs) - dated,
            "years_covered": len(years),
            "min_year": min(years) if years else None,
            "max_year": max(years) if years else None,
            "as_of": str(now),
        },
        "by_month": by_month,
        "by_quarter": by_quarter,
        "season_summary": {
            "monthly_avg": monthly_avg,
            "peak_months": [r["month"] for r in by_month if r["season"] == "peak"],
            "off_months": [r["month"] for r in by_month if r["season"] == "off"],
            "peak_ratio": PEAK_RATIO,
            "off_ratio": OFF_RATIO,
            "enough_sample": enough,
            "min_dated_jobs": min_dated,
        },
        "current": current,
        "notes": notes,
    }