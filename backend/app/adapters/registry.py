"""ats_type → Adapter 注册表。"""

from __future__ import annotations

from app.adapters.base import AtsAdapter
from app.adapters.ashby import AshbyAdapter
from app.adapters.greenhouse import GreenhouseAdapter
from app.adapters.lever import LeverAdapter
from app.adapters.netease import NeteaseAdapter
from app.adapters.official_site import OfficialSiteAdapter
from app.adapters.smartrecruiters import SmartRecruitersAdapter
from app.adapters.workday import WorkdayAdapter

ADAPTERS: dict[str, type[AtsAdapter]] = {
    WorkdayAdapter.ats_type: WorkdayAdapter,
    GreenhouseAdapter.ats_type: GreenhouseAdapter,
    LeverAdapter.ats_type: LeverAdapter,
    NeteaseAdapter.ats_type: NeteaseAdapter,
    AshbyAdapter.ats_type: AshbyAdapter,
    SmartRecruitersAdapter.ats_type: SmartRecruitersAdapter,
    OfficialSiteAdapter.ats_type: OfficialSiteAdapter,  # 兜底骨架，未有代表公司
}


def get_adapter(ats_type: str, client=None) -> AtsAdapter:
    cls = ADAPTERS.get(ats_type)
    if cls is None:
        raise KeyError(f"未注册的 ATS 类型: {ats_type}")
    return cls(client=client)
