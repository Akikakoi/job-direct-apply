"""ATS 适配器抽象基类与数据结构（开发文档 §5.1）。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import ClassVar

import httpx

from app.core.config import settings
from app.models import Company


@dataclass
class RawJob:
    """适配器 discover 的原始产物。"""

    payload: dict
    company_slug: str = ""
    feed_url: str | None = None


@dataclass
class NormalizedJob:
    """§3.2 标准化职位 Schema。"""

    external_id: str
    title: str
    apply_url: str
    source: str
    city: str | None = None
    skills: list[str] = field(default_factory=list)
    experience_min: int | None = None
    degree_req: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    description: str | None = None
    can_auto_apply: bool = False
    publish_date: date | None = None

    def to_row(self) -> dict:
        """供 upsert 使用的字段字典（不含 company_id/状态字段）。"""
        return {
            "external_id": self.external_id,
            "title": self.title,
            "city": self.city,
            "skills": list(self.skills),
            "experience_min": self.experience_min,
            "degree_req": self.degree_req,
            "salary_min": self.salary_min,
            "salary_max": self.salary_max,
            "salary_currency": self.salary_currency,
            "description": self.description,
            "apply_url": self.apply_url,
            "can_auto_apply": self.can_auto_apply,
            "source": self.source,
            "publish_date": self.publish_date,
        }


class AtsAdapter(ABC):
    """统一接口：discover 拉原始数据，normalize 输出标准化职位。

    实现约定：
    - HTTP 层（discover）与字段映射层（normalize_raw）分离，便于单测不触网；
    - httpx.Client 可注入（测试用 httpx.MockTransport）。
    """

    ats_type: ClassVar[str] = ""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(
            timeout=settings.http_timeout_s,
            headers={"User-Agent": settings.ua, "Accept-Language": "en-US"},
            follow_redirects=True,
        )

    @abstractmethod
    def discover(self, company: Company) -> list[RawJob]: ...

    @abstractmethod
    def normalize_raw(self, raw: RawJob) -> NormalizedJob:
        """§3.2 字段映射。"""

    def normalize_all(self, company: Company) -> list[NormalizedJob]:
        return [self.normalize_raw(r) for r in self.discover(company)]
