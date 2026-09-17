"""Workday 适配器。

接口要点（开发文档 §5.3 踩坑实录，实现必须遵守）：
- 真实接口：POST https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
- ① limit 上限 20，传 21 会静默返回空数组（HTTP 200、total 不变）；
- ② 单站点单次搜索 2000 条封顶，超出需按 searchText/location 切分；
- ③ {site} 大小写敏感，拼错返回 404；site 从 career site URL 路径解析（剔除 locale 段）。
"""

from __future__ import annotations

import httpx

from app.adapters.base import AtsAdapter, RawJob, NormalizedJob
from app.core.config import settings
from app.models import Company


def parse_workday_site(feed_url: str) -> tuple[str, str, str]:
    """从 career site URL 分解 (tenant, shard, site)。

    例：https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite
        → ("nvidia", "wd5", "NVIDIAExternalCareerSite")
    """
    url = httpx.URL(feed_url)
    host_parts = url.host.split(".")
    if len(host_parts) < 3 or not host_parts[1].startswith("wd"):
        raise ValueError(f"不是合法的 Workday career site URL: {feed_url}")
    tenant, shard = host_parts[0], host_parts[1]
    site = url.path.strip("/")
    if "/" in site or not site:
        # 形如 /zh-CN/ExternalSite 或多段路径：取最后一个非 locale 段
        segs = [s for s in url.path.split("/") if s and not s.lower() in ("en-us", "zh-cn", "en-gb")]
        if not segs:
            raise ValueError(f"无法从 URL 解析 site: {feed_url}")
        site = segs[-1]
    return tenant, shard, site


class WorkdayAdapter(AtsAdapter):
    ats_type = "workday"

    def discover(self, company: Company) -> list[RawJob]:
        if not company.feed_url:
            raise ValueError(f"company {company.slug} 缺少 feed_url")
        tenant, shard, site = parse_workday_site(company.feed_url)
        base = f"https://{tenant}.{shard}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
        jobs_url = f"{base}/jobs"

        raws: list[RawJob] = []
        offset = 0
        while True:
            # 坑①：limit 必须 ≤ 20，否则静默返空
            body = {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""}
            resp = self._client.post(jobs_url, json=body)
            resp.raise_for_status()
            data = resp.json()
            postings = data.get("jobPostings") or []
            if not postings:
                break  # 坑②：部分租户 total 虚报，空页即真实终点
            for p in postings:
                raws.append(RawJob(payload=p, company_slug=company.slug, feed_url=company.feed_url))
            offset += 20
            total = int(data.get("total") or 0)
            if offset >= min(total, settings.workday_max_total):
                break
            if offset >= settings.workday_max_pages * 20:
                break  # 坑②兜底：翻页安全上限
        return raws

    def normalize_raw(self, raw: RawJob) -> NormalizedJob:
        p = raw.payload
        path: str = p.get("externalPath") or ""
        if not path:
            raise ValueError("Workday jobPostings 缺少 externalPath")
        #坑③相关：site 大小写敏感只影响请求，这里只做 ID/URL 提取
        seg = path.rstrip("/").split("/")[-1]
        external_id = seg.split("_")[-1] if "_" in seg else seg
        host = httpx.URL(raw.feed_url).host if raw.feed_url else None
        apply_url = f"https://{host}{path}" if host else path

        bullet_fields = [str(b).strip().lower() for b in (p.get("bulletFields") or []) if str(b).strip()]

        return NormalizedJob(
            external_id=external_id,
            title=str(p.get("title") or "").strip(),
            city=(p.get("locationsText") or None),
            skills=bullet_fields,
            apply_url=apply_url,
            source=self.ats_type,
            can_auto_apply=False,
        )
