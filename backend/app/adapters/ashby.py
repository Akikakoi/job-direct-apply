"""Ashby 适配器：posting-api 公开 JSON，无需鉴权（2026-09-18 实测）。

GET https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true
返回 {apiVersion, jobs: [...]}，单页全量、无分页；
job 字段含 id/title/department/location/isRemote/jobUrl/applyUrl/
descriptionPlain/publishedAt（ISO）——列表即带全文，零 N+1。
2026-09-18 实测：ashby 73 条 / elevenlabs 236 / linear 32 / watershed 33（vercel 0）。
"""

from __future__ import annotations

import re
from datetime import date

from app.adapters.base import AtsAdapter, NormalizedJob, RawJob
from app.models import Company


def parse_ashby_org(feed_url: str | None, fallback: str) -> str:
    if feed_url:
        m = re.search(r"jobs\.ashbyhq\.com/([^/?]+)", feed_url)
        if m:
            return m.group(1)
    return fallback  # 约定：org 与 company.slug 一致


class AshbyAdapter(AtsAdapter):
    ats_type = "ashby"

    def discover(self, company: Company) -> list[RawJob]:
        org = parse_ashby_org(company.feed_url, company.slug)
        url = f"https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true"
        resp = self._client.get(url)
        resp.raise_for_status()
        jobs = resp.json().get("jobs") or []
        return [
            RawJob(payload=j, company_slug=company.slug, feed_url=url)
            for j in jobs
            if isinstance(j, dict) and j.get("isListed", True)
        ]

    def normalize_raw(self, raw: RawJob) -> NormalizedJob:
        j = raw.payload
        publish_date: date | None = None
        published = j.get("publishedAt")
        if published:
            try:
                publish_date = date.fromisoformat(str(published)[:10])
            except ValueError:
                publish_date = None
        return NormalizedJob(
            external_id=str(j.get("id") or ""),
            title=str(j.get("title") or "").strip(),
            city=(j.get("location") or None),
            skills=[],
            description=j.get("descriptionPlain") or None,
            apply_url=str(j.get("jobUrl") or j.get("applyUrl") or ""),
            source=self.ats_type,
            publish_date=publish_date,
        )
