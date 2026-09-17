"""Lever 适配器：api.lever.co 公开 JSON，无需鉴权。

GET https://api.lever.co/v0/postings/{slug}?mode=json
字段：[{id, text, hostedUrl, createdAt(ms), categories.{location, team, commitment}, descriptionPlain}]
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

from app.adapters.base import AtsAdapter, NormalizedJob, RawJob
from app.models import Company


def parse_lever_slug(feed_url: str | None, fallback: str) -> str:
    if feed_url:
        m = re.search(r"/postings/([^/?]+)", feed_url) or re.search(r"jobs\.lever\.co/([^/?]+)", feed_url)
        if m:
            return m.group(1)
    return fallback  # 约定：slug 与 company.slug 一致


class LeverAdapter(AtsAdapter):
    ats_type = "lever"

    def discover(self, company: Company) -> list[RawJob]:
        slug = parse_lever_slug(company.feed_url, company.slug)
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        resp = self._client.get(url)
        resp.raise_for_status()
        data = resp.json()
        return [
            RawJob(payload=j, company_slug=company.slug, feed_url=url)
            for j in (data or [])
            if isinstance(j, dict)
        ]

    def normalize_raw(self, raw: RawJob) -> NormalizedJob:
        j = raw.payload
        cats = j.get("categories") or {}
        publish_date: date | None = None
        created_ms = j.get("createdAt")
        if created_ms:
            try:
                publish_date = datetime.fromtimestamp(int(created_ms) / 1000, tz=timezone.utc).date()
            except (ValueError, OverflowError):
                publish_date = None
        return NormalizedJob(
            external_id=str(j.get("id") or ""),
            title=str(j.get("text") or "").strip(),
            city=(cats.get("location") or None),
            skills=[],
            description=j.get("descriptionPlain") or None,
            apply_url=str(j.get("hostedUrl") or ""),
            source=self.ats_type,
            publish_date=publish_date,
        )
