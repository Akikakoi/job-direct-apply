"""SmartRecruiters 适配器：公开 API v1，无需鉴权（2026-09-18 实测）。

GET https://api.smartrecruiters.com/v1/companies/{company}/postings?limit=100&offset=N
返回 {offset, limit, totalFound, content: [...]}；limit=100 可用（Equinox totalFound=736）。
列表项含 id/name/location/department/experienceLevel/releasedDate，但**不含**职位描述。

详情补齐（N+1）：settings.sr_fetch_details=True 时每次采集最多补
sr_detail_cap 条——GET /postings/{id} → jobAd.sections（dict，text 为 HTML，
剥标签入 descriptionPlain），并换用详情里的带 slug applyUrl。
"""

from __future__ import annotations

import html as html_mod
import re
import time
from datetime import date
from urllib.parse import quote

from app.adapters.base import AtsAdapter, NormalizedJob, RawJob
from app.core.config import settings
from app.models import Company

_PAGE_SIZE = 100
_MAX_PAGES = 10  # 1000 条安全上限


def parse_sr_company(feed_url: str | None, fallback: str) -> str:
    if feed_url:
        m = (
            re.search(r"jobs\.smartrecruiters\.com/([^/?]+)", feed_url)
            or re.search(r"/companies/([^/?]+)/postings", feed_url)
        )
        if m:
            return m.group(1)
    return fallback


def html_to_text(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _description_from_sections(sections: dict) -> str:
    parts = []
    for key, sec in sections.items():
        if not isinstance(sec, dict):
            continue
        text = html_to_text(sec.get("text") or "")
        if text:
            parts.append(f"{sec.get('title') or key}: {text}")
    return "\n\n".join(parts)


def _city_from_location(loc: dict) -> str | None:
    if not isinstance(loc, dict):
        return None
    if loc.get("remote"):
        return "Remote"
    parts = [str(loc.get(k)) for k in ("city", "region", "country") if loc.get(k)]
    return ", ".join(parts) or None


class SmartRecruitersAdapter(AtsAdapter):
    ats_type = "smartrecruiters"

    def discover(self, company: Company) -> list[RawJob]:
        ident = parse_sr_company(company.feed_url, company.slug)
        base = f"https://api.smartrecruiters.com/v1/companies/{quote(ident)}/postings"
        raws: list[RawJob] = []
        offset = 0
        for _page in range(_MAX_PAGES):
            resp = self._client.get(base, params={"limit": _PAGE_SIZE, "offset": offset})
            resp.raise_for_status()
            data = resp.json()
            content = data.get("content") or []
            raws.extend(
                RawJob(payload=j, company_slug=company.slug, feed_url=base)
                for j in content
                if isinstance(j, dict)
            )
            offset += len(content)
            if offset >= int(data.get("totalFound") or 0) or not content:
                break

        if settings.sr_fetch_details:
            self._enrich_details(ident, raws)
        return raws

    def _enrich_details(self, ident: str, raws: list[RawJob]) -> None:
        """详情补齐：最多 sr_detail_cap 条（N+1，礼貌限速 0.1s/条）。"""
        base = f"https://api.smartrecruiters.com/v1/companies/{quote(ident)}/postings"
        for raw in raws[: max(settings.sr_detail_cap, 0)]:
            pid = raw.payload.get("id")
            try:
                resp = self._client.get(f"{base}/{pid}")
                if resp.status_code != 200:
                    continue
                detail = resp.json()
                sections = (detail.get("jobAd") or {}).get("sections") or {}
                description = _description_from_sections(sections)
                if description:
                    raw.payload["descriptionPlain"] = description
                if detail.get("applyUrl"):
                    raw.payload["applyUrl"] = detail["applyUrl"]
            except Exception:
                continue  # 单条失败不阻塞整批
            time.sleep(0.1)

    def normalize_raw(self, raw: RawJob) -> NormalizedJob:
        j = raw.payload
        ident = parse_sr_company(raw.feed_url, raw.company_slug)
        publish_date: date | None = None
        released = j.get("releasedDate")
        if released:
            try:
                publish_date = date.fromisoformat(str(released)[:10])
            except ValueError:
                publish_date = None
        return NormalizedJob(
            external_id=str(j.get("id") or ""),
            title=str(j.get("name") or "").strip(),
            city=_city_from_location(j.get("location") or {}),
            skills=[],
            description=j.get("descriptionPlain") or None,
            apply_url=str(j.get("applyUrl") or f"https://jobs.smartrecruiters.com/{quote(ident)}/{j.get('id')}"),
            source=self.ats_type,
            publish_date=publish_date,
        )
