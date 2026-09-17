"""网易招聘适配器 —— OfficialSiteAdapter 家族首个落地实现（§5.3）。

站点特征（2026-09-16 实测）：
- hr.163.com 为 React SPA，/robots.txt 返回 HTML 回退页（无真实 robots），
  按惯例视为允许，仍遵守低频策略（official_site_interval_min=720）；
- 数据走站点自有公开 JSON 接口，无需鉴权、无加密：

  POST {base}/api/hr163/position/queryPage
    body: {"currentPage": N, "pageSize": 50, "keyword": "",
           "workPlaceList": [], "jobTypeId": ""}
    resp: {"code": 200, "data": {"pages": 53, "total": 2641, "list": [...]}}

- pageSize=50 实测可用；列表项自带 description/requirement 全文，
  无需逐条调用详情接口（避免 2600+ 次额外请求）；
- 职位详情页 URL 形如 https://hr.163.com/job-detail.html?id={id}&lang=zh；
- 列表项 beeUrl 实测多为 None，兜底自建详情链接。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

from app.adapters.base import NormalizedJob, RawJob
from app.adapters.official_site import OfficialSiteAdapter
from app.models import Company

DEFAULT_BASE = "https://hr.163.com"


def parse_netease_base(feed_url: str | None, site_url: str | None = None) -> str:
    """取数据源根地址（去尾斜杠），feed_url 优先于 site_url。"""
    base = (feed_url or site_url or DEFAULT_BASE).rstrip("/")
    return base or DEFAULT_BASE


def parse_work_years(text: str | None) -> int | None:
    """从 reqWorkYearsName 提取最低经验年数：'3-5年'→3，'5年以上'→5，
    '1年以下'→0，'不限'/'应届'/None→None。"""
    if not text or "不限" in text or "应届" in text:
        return None
    m = re.search(r"(\d+)\s*-\s*\d+年", text)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)年以上", text)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)年以下", text)
    if m:
        return 0
    return None


def _degree(text: str | None) -> str | None:
    if not text or text == "不限":
        return None
    return text


def _merge_description(description: str | None, requirement: str | None) -> str | None:
    parts = [p.strip() for p in (description, requirement) if p and p.strip()]
    if not parts:
        return None
    return "\n\n【任职要求】\n".join(parts) if len(parts) == 2 else parts[0]


class NeteaseAdapter(OfficialSiteAdapter):
    """网易招聘：站点自有公开 JSON 接口（详见模块 docstring 实测结论）。"""

    ats_type = "netease"
    PAGE_SIZE = 50   # 实测可用上限（10 亦可）
    MAX_PAGES = 100  # 翻页安全上限（50 × 100 = 5000，覆盖当前 2641 条）

    def discover(self, company: Company) -> list[RawJob]:
        base = parse_netease_base(company.feed_url, company.site_url)
        url = f"{base}/api/hr163/position/queryPage"
        raws: list[RawJob] = []
        page = 1
        while page <= self.MAX_PAGES:
            resp = self._client.post(
                url,
                json={
                    "currentPage": page,
                    "pageSize": self.PAGE_SIZE,
                    "keyword": "",
                    "workPlaceList": [],
                    "jobTypeId": "",
                },
            )
            resp.raise_for_status()
            data = (resp.json() or {}).get("data") or {}
            items = data.get("list") or []
            if not items:
                break
            raws.extend(
                RawJob(payload=j, company_slug=company.slug, feed_url=base)
                for j in items
            )
            pages = int(data.get("pages") or 0)
            if page >= pages:
                break
            page += 1
        return raws

    def normalize_raw(self, raw: RawJob) -> NormalizedJob:
        j = raw.payload
        jid = str(j.get("id") or "")
        base = (raw.feed_url or DEFAULT_BASE).rstrip("/")
        apply_url = j.get("beeUrl") or f"{base}/job-detail.html?id={jid}&lang=zh"
        places = j.get("workPlaceNameList") or []
        city = "、".join(str(p) for p in places if p) or None
        ms = j.get("updateTime")
        publish_date: date | None = None
        if ms:
            try:
                publish_date = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date()
            except (ValueError, OverflowError, OSError):
                publish_date = None
        return NormalizedJob(
            external_id=jid,
            title=str(j.get("name") or "").strip(),
            city=city,
            skills=[],  # 描述正文技能抽取属 P1.3（采集时统一打标签）
            experience_min=parse_work_years(j.get("reqWorkYearsName")),
            degree_req=_degree(j.get("reqEducationName")),
            description=_merge_description(j.get("description"), j.get("requirement")),
            apply_url=str(apply_url),
            source=self.ats_type,
            publish_date=publish_date,
        )
