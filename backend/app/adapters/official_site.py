"""官网兜底适配器（OfficialSiteAdapter）。

P1 定稿（§5.3）：网易已评估并落地为 NeteaseAdapter（本模块是其基类，
提供 robots 校验与"默认允许 + 低频"约定）；大疆经实测走 Moka 系统、
接口响应整体加密，主动解密属绕过反爬，暂缓（§5.3 有完整证据）。
其余站点逐站评估后再实现，遵守：低频、白名单、不绕反爬。
"""

from __future__ import annotations

from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from app.adapters.base import AtsAdapter, NormalizedJob, RawJob
from app.models import Company


def parse_robots(text: str, url: str, ua: str = "*") -> bool:
    """判断 url 是否被 robots.txt 允许。纯解析，不触网，便于测试。"""
    rp = RobotFileParser()
    rp.parse(text.splitlines())
    return rp.can_fetch(ua, url)


async def fetch_robots_allowed(base_url: str, target_url: str, ua: str, client) -> bool:
    """拉取 robots.txt 并判断（生产路径）。"""
    parsed = urlparse(base_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    resp = await client.get(robots_url)
    if resp.status_code >= 400:
        return True  # 无 robots 视为允许，但仍遵守低频策略
    return parse_robots(resp.text, target_url, ua)


class OfficialSiteAdapter(AtsAdapter):
    ats_type = "official_site"

    def discover(self, company: Company) -> list[RawJob]:
        raise NotImplementedError(
            "OfficialSiteAdapter 规则解析待实现：需先完成 "
            f"{company.site_url} 的 robots 与页面结构评估（P1 任务项）"
        )

    def normalize_raw(self, raw: RawJob) -> NormalizedJob:
        raise NotImplementedError("OfficialSiteAdapter.normalize_raw 待实现")
