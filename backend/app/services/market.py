"""市场洞察报告（§12.6 P5 ①）：城市 / 技能 / 薪资趋势，纯聚合 + 脱敏。

数据源只有 `jobs`（`status=active`），不引入新表、不依赖外部数据：
- **城市分布**：按区（国内/海外，与 §4.2 的 region 口径同源）分组计数，城市取
  `city_keys` 首段（采集侧已规范化多城市/别名），回退 `city` 首个分隔段；
- **技能需求**：`jobs.skills`（已归一的 skill_tags 标准标签）计数与占比；
- **薪资区间**：只统计 `salary_min`/`salary_max` 齐全的职位，按币种分组给
  中位数与四分位（**不暴露单条薪资**，聚合即脱敏）；
- **发布趋势**：按 `publish_date` 的月份计数（缺失回退 `created_at`），窗口内
  补齐零值月份，便于前端直接画折线。

**脱敏口径（B 端交付的前置条件）**：所有分桶计数 < `min_sample`（默认 3）的
一律**不单独输出**，只累加进 `suppressed` 计数——小样本分桶（如"某城市 1 条"）
配合已知信息可反推到具体公司/个人，故按 k-匿名下限截断。报告全篇不含公司名、
职位标题与申请链接，天然满足"B 端脱敏数据"这一交付形态。

**本项已收敛的边界（离线可测部分）**：报告生成 + 脱敏 + 数值口径（本模块 +
`GET /api/insights/market`）。**剩余条件（非代码）**：C 端订阅的推送渠道与频次
（可复用 `notify.py` 的邮件/IM 通道，但订阅名单与计费属运营配置）、B 端数据
交付（合同/脱敏协议/导出格式）。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job

# 城市串内的多值分隔符（与 services.city 的拆分口径一致：分号/顿号/竖线/斜杠）
_CITY_SEPS = (";", "；", "、", "|", "/", ",")


def domestic_sources() -> tuple[str, ...]:
    """国内源清单——与 §4.2 `/api/recommend?region=` 的唯一事实来源保持一致。

    懒导入 `app.main`：main 侧对 services 的引用也都在函数内，避免模块级循环。
    """
    from app.main import DOMESTIC_SOURCES

    return DOMESTIC_SOURCES


def region_of(job: Job, domestic: tuple[str, ...] | None = None) -> str:
    """职位所属区：cn（国内源）/ overseas（其余，含 `source IS NULL` 脏数据）。

    口径与 §4.2 完全一致——`NOT IN` 遇 NULL 会整条件落空，故显式把 NULL 归海外。
    """
    domestic = domestic if domestic is not None else domestic_sources()
    return "cn" if job.source in domestic else "overseas"


def first_city(job: Job) -> str | None:
    """职位的首个城市（city_keys 优先，回退 city 首个分隔段）；无有效城市 → None。"""
    keys = (job.city_keys or "").strip()
    if keys:
        for sep in ("|",):
            if sep in keys:
                keys = keys.split(sep)[0]
        keys = keys.strip()
        if keys:
            return keys
    city = (job.city or "").strip()
    if not city or city.upper() == "N/A":
        return None
    for sep in _CITY_SEPS:
        if sep in city:
            city = city.split(sep)[0]
    city = city.strip()
    return city or None


def median(values: list[float]) -> float | None:
    """中位数（偶数取中间两值均值）；空列表 → None。"""
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return round(ordered[mid], 2)
    return round((ordered[mid - 1] + ordered[mid]) / 2, 2)


def percentile(values: list[float], q: float) -> float | None:
    """线性插值分位（q∈[0,1]）；空列表 → None。"""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    pos = q * (len(ordered) - 1)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return round(ordered[low] + (ordered[high] - ordered[low]) * frac, 2)


def _month_key(job: Job) -> str | None:
    """职位发布月份（YYYY-MM）：publish_date 优先，缺失回退 created_at。"""
    if job.publish_date is not None:
        d = job.publish_date
        return f"{d.year:04d}-{d.month:02d}"
    if job.created_at is not None:
        return f"{job.created_at.year:04d}-{job.created_at.month:02d}"
    return None


def _month_window(now: date, months: int) -> list[str]:
    """截至 now 的连续 months 个月份键（升序），用于趋势零值补齐。"""
    keys: list[str] = []
    y, m = now.year, now.month
    for _ in range(max(months, 1)):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(keys))


def _bucket(counter: Counter, min_sample: int, key_name: str) -> tuple[list[dict], int]:
    """分桶输出 + k-匿名截断：计数 < min_sample 的不单独输出，只计数。"""
    rows: list[dict] = []
    suppressed = 0
    for key, count in counter.most_common():
        if count < min_sample:
            suppressed += 1
            continue
        rows.append({key_name: key, "count": count})
    return rows, suppressed


def market_report(
    session: Session,
    region: str = "all",
    top: int = 10,
    min_sample: int = 3,
    months: int = 12,
    now: date | None = None,
) -> dict:
    """汇总市场洞察报告；region=all|cn|overseas，非法值由调用方（API 层）拦。"""
    now = now or datetime.utcnow().date()
    domestic = domestic_sources()
    jobs = session.execute(select(Job).where(Job.status == "active")).scalars().all()
    if region in ("cn", "overseas"):
        jobs = [j for j in jobs if region_of(j, domestic) == region]

    city_counter: dict[str, Counter] = {"cn": Counter(), "overseas": Counter()}
    skill_counter: Counter = Counter()
    salary_groups: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"min": [], "max": []})
    trend_counter: Counter = Counter()
    with_salary = 0
    with_skills = 0
    unknown_city = 0

    for job in jobs:
        city = first_city(job)
        if city is None:
            unknown_city += 1
        else:
            city_counter[region_of(job, domestic)][city] += 1

        if job.skills:
            with_skills += 1
            skill_counter.update(str(s) for s in job.skills)

        if job.salary_min is not None and job.salary_max is not None:
            with_salary += 1
            currency = (job.salary_currency or "unknown").upper()
            salary_groups[currency]["min"].append(float(job.salary_min))
            salary_groups[currency]["max"].append(float(job.salary_max))

        month = _month_key(job)
        if month:
            trend_counter[month] += 1

    cities: dict[str, list[dict]] = {}
    suppressed = 0
    for region_key in ("cn", "overseas"):
        rows, hidden = _bucket(city_counter[region_key], min_sample, "city")
        cities[region_key] = rows[:top]
        suppressed += hidden

    skill_rows, hidden_skills = _bucket(skill_counter, min_sample, "skill")
    suppressed += hidden_skills
    skills = [
        {**row, "share": round(row["count"] / len(jobs), 4) if jobs else None}
        for row in skill_rows[:top]
    ]

    salary = {
        currency: {
            "count": len(vals["max"]),
            "min_median": median(vals["min"]),
            "max_median": median(vals["max"]),
            "p25": percentile(vals["max"], 0.25),
            "p75": percentile(vals["max"], 0.75),
        }
        for currency, vals in sorted(salary_groups.items())
        if len(vals["max"]) >= min_sample
    }

    window = _month_window(now, months)
    trend = [{"month": m, "count": trend_counter.get(m, 0)} for m in window]

    return {
        "scope": {
            "region": region,
            "total_jobs": len(jobs),
            "with_skills": with_skills,
            "with_salary": with_salary,
            "unknown_city": unknown_city,
            "as_of": str(now),
        },
        "cities": cities,
        "skills": skills,
        "salary": salary,
        "trend": trend,
        "anonymity": {"min_sample": min_sample, "suppressed_buckets": suppressed},
        "notes": [
            "数据源为 active 职位快照，脱敏口径：分桶计数 < min_sample 不单独输出，只计入 suppressed_buckets。",
            "薪资仅统计 min/max 齐全的职位并按币种分组，跨币种不做汇率换算。",
            "趋势按 publish_date 归月，缺失时回退 created_at（会聚集到采集月份，近月偏高属预期）。",
            "报告不含公司名/职位标题/申请链接，可直接用于 B 端脱敏交付。",
        ],
    }