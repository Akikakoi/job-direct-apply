"""Greenhouse 适配器：boards-api 公开 JSON，无需鉴权。

GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs
字段：jobs[{id, title, absolute_url, location.name, updated_at, ...}]
"""

from __future__ import annotations

import re
from datetime import date, datetime

from app.adapters.base import AtsAdapter, NormalizedJob, RawJob
from app.models import Company


def parse_greenhouse_token(feed_url: str) -> str:
    m = re.search(r"/boards/([^/]+)/jobs", feed_url)
    if not m:
        raise ValueError(f"无法从 URL 解析 Greenhouse board token: {feed_url}")
    return m.group(1)


class GreenhouseAdapter(AtsAdapter):
    ats_type = "greenhouse"

    def discover(self, company: Company) -> list[RawJob]:
        token = parse_greenhouse_token(company.feed_url or "")
        url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
        resp = self._client.get(url)
        resp.raise_for_status()
        data = resp.json()
        return [
            RawJob(payload=j, company_slug=company.slug, feed_url=url)
            for j in (data.get("jobs") or [])
        ]

    def normalize_raw(self, raw: RawJob) -> NormalizedJob:
        j = raw.payload
        publish_date: date | None = None
        updated = j.get("updated_at")
        if updated:
            try:
                publish_date = datetime.fromisoformat(str(updated).replace("Z", "+00:00")).date()
            except ValueError:
                publish_date = None
        location = j.get("location") or {}
        return NormalizedJob(
            external_id=str(j.get("id") or ""),
            title=str(j.get("title") or "").strip(),
            city=(location.get("name") or None),
            skills=[],
            apply_url=str(j.get("absolute_url") or ""),
            source=self.ats_type,
            publish_date=publish_date,
        )
