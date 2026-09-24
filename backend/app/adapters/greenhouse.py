"""Greenhouse 适配器：boards-api 公开 JSON，无需鉴权。

GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
字段：jobs[{id, title, absolute_url, location.name, updated_at, content, ...}]

`content=true` 让列表接口直接带上职位全文（**实体转义的 HTML**），
一次请求补齐 800 条 description——此前不带该参数导致 greenhouse 全量无
description，语义分为 0、排名被系统性压低（§12.7 #6）。
"""

from __future__ import annotations

import html as html_mod
import re
from datetime import date, datetime

from app.adapters.base import AtsAdapter, NormalizedJob, RawJob
from app.models import Company


def parse_greenhouse_token(feed_url: str) -> str:
    m = re.search(r"/boards/([^/]+)/jobs", feed_url)
    if not m:
        raise ValueError(f"无法从 URL 解析 Greenhouse board token: {feed_url}")
    return m.group(1)


def content_to_text(content: str) -> str:
    """content 是实体转义后的 HTML（形如 `&lt;div&gt;…`）。

    必须先 unescape 再剥标签：顺序反了正则找不到任何真实标签，整段 HTML
    会原样入库（smartrecruiters 的 html_to_text 是"先剥后 unescape"，
    只适用于未转义场景，这里不能复用）。
    """
    text = html_mod.unescape(content or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class GreenhouseAdapter(AtsAdapter):
    ats_type = "greenhouse"

    def discover(self, company: Company) -> list[RawJob]:
        token = parse_greenhouse_token(company.feed_url or "")
        url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
        resp = self._client.get(url, params={"content": "true"})
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
            description=content_to_text(j.get("content") or "") or None,
            apply_url=str(j.get("absolute_url") or ""),
            source=self.ats_type,
            publish_date=publish_date,
        )
