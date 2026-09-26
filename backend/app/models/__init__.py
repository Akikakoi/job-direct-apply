"""全部 ORM 模型，对应开发文档 §3 数据模型。

可移植性约定：
- 主键：Integer().with_variant(BigInteger, "postgresql")，SQLite 下正常自增；
- JSON：JSON().with_variant(JSONB, "postgresql")；
- PGVector 向量列（jobs.vector）不进 ORM：SQLite 无此类型，由 services/vector_store.py
  在 PG 上 raw SQL 幂等建列与读写（§7 二期，列不定维，维度门禁在应用层）。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

BIGPK = Integer().with_variant(BigInteger(), "postgresql")
JSONType = JSON().with_variant(JSONB(), "postgresql")


class Company(Base):
    """§3.1 公司 → ATS 站点映射（冷启动核心资产）。"""

    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(BIGPK, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    ats_type: Mapped[str] = mapped_column(Text, nullable=False)  # workday|greenhouse|lever|official_site|...
    site_url: Mapped[str | None] = mapped_column(Text)
    feed_url: Mapped[str | None] = mapped_column(Text)  # 公开 JSON API / Workday career site URL
    locale: Mapped[str] = mapped_column(Text, default="zh-CN")
    auth_type: Mapped[str] = mapped_column(Text, default="public")  # public|oauth|none
    fetch_policy: Mapped[dict] = mapped_column(JSONType, default=lambda: {"interval_min": 360})
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Job(Base):
    """§3.2 标准化职位。去重主键 (company_id, external_id)。"""

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("company_id", "external_id", name="uq_jobs_company_external"),
        Index("idx_jobs_source", "source"),
        Index("idx_jobs_city_status", "city", "status"),
    )

    id: Mapped[int] = mapped_column(BIGPK, primary_key=True, autoincrement=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    city: Mapped[str | None] = mapped_column(Text)
    # 规范化多城市索引串（city.py city_keys 生成，"|" 连接；查询侧双列 LIKE）
    city_keys: Mapped[str | None] = mapped_column(Text)
    skills: Mapped[list] = mapped_column(JSONType, default=list)  # skill_tags 标准标签
    experience_min: Mapped[int | None] = mapped_column(Integer)
    degree_req: Mapped[str | None] = mapped_column(Text)  # bachelor|master|phd|na
    salary_min: Mapped[Decimal | None] = mapped_column(Numeric)
    salary_max: Mapped[Decimal | None] = mapped_column(Numeric)
    salary_currency: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    apply_url: Mapped[str] = mapped_column(Text, nullable=False)
    can_auto_apply: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str | None] = mapped_column(Text)
    publish_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, default="active")  # active|closed|expired
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MatchWeightVersion(Base):
    """§12.5 反馈回灌闭环：被采纳的匹配权重版本（历史即审计轨迹）。

    生效权重 = 最新一行；表空时回退 `settings.MATCH_W_*`（旧部署行为不变）。
    落库而不写 `.env`：权重是随反馈演进的数据，要可审计、可回滚；`.env` 是部署配置。
    """

    __tablename__ = "match_weight_versions"

    id: Mapped[int] = mapped_column(BIGPK, primary_key=True, autoincrement=True)
    weights: Mapped[dict] = mapped_column(JSONType, nullable=False)  # {skill,city,exp,role,alpha,beta}
    source: Mapped[str] = mapped_column(Text, nullable=False)  # manual|auto
    note: Mapped[str | None] = mapped_column(Text)
    sample_size: Mapped[int | None] = mapped_column(Integer)  # 采纳时的有反馈简历数
    baseline_ndcg: Mapped[float | None] = mapped_column(Float)
    new_ndcg: Mapped[float | None] = mapped_column(Float)
    rematched: Mapped[int | None] = mapped_column(Integer)  # 采纳后重算的 match_scores 行数
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    """§3.7 账号体系。各业务表 user_id 引用本表。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BIGPK, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, default="user")  # user|admin
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Resume(Base):
    """§3.3 简历。is_active：同用户多份简历时标记当前生效的一份。"""

    __tablename__ = "resumes"

    id: Mapped[int] = mapped_column(BIGPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    file_path: Mapped[str | None] = mapped_column(Text)
    raw_text: Mapped[str | None] = mapped_column(Text)
    profile: Mapped[dict | None] = mapped_column(JSONType)
    lang: Mapped[str] = mapped_column(Text, default="zh")
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MatchScore(Base):
    """§3.4 匹配分。"""

    __tablename__ = "match_scores"
    __table_args__ = (UniqueConstraint("resume_id", "job_id", name="uq_match_resume_job"),)

    id: Mapped[int] = mapped_column(BIGPK, primary_key=True, autoincrement=True)
    resume_id: Mapped[int | None] = mapped_column(ForeignKey("resumes.id"))
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"))
    # 精度 Numeric(6,4)（第二十七轮）：两个小数位会让人造并列——海外 1918 条里最大
    # 同分块曾达 855 条，排序实际退化成"谁先入库"。打分侧本就 round(..., 4)，四位小数
    # 才能把 L3 分档与权重归一的区分度落到库里（改列需 alembic 0005 + 全量重算）。
    rule_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    vec_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    llm_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    final_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    explain: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Application(Base):
    """§3.5 投递 + 反馈闭环。"""

    __tablename__ = "applications"
    __table_args__ = (
        Index("idx_apps_user", "user_id"),
        Index("idx_apps_status", "status"),
    )

    id: Mapped[int] = mapped_column(BIGPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    resume_id: Mapped[int | None] = mapped_column(ForeignKey("resumes.id"))
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"))
    mode: Mapped[str | None] = mapped_column(Text)  # direct_link|semi_auto
    status: Mapped[str] = mapped_column(Text, nullable=False, default="submitted")
    apply_url: Mapped[str | None] = mapped_column(Text)
    external_ref: Mapped[str | None] = mapped_column(Text)
    authorized: Mapped[bool] = mapped_column(Boolean, default=False)
    # §14 ② 同意留痕：授权时间 + 所同意的政策版本（app/services/legal.py 的 POLICY_VERSION）。
    # 可空：0003 迁移前的历史行没有留痕，如实为"未知"而不回填假数据。
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consent_version: Mapped[str | None] = mapped_column(Text)
    supplier_new: Mapped[str | None] = mapped_column(Text)
    # §12.6 P5 ② 面试时间（用户登记，可空）：面试陪伴的定时催进据此触发
    interview_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class FeedbackLog(Base):
    """§3.6 结果回灌。"""

    __tablename__ = "feedback_log"

    id: Mapped[int] = mapped_column(BIGPK, primary_key=True, autoincrement=True)
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id"))
    outcome: Mapped[str | None] = mapped_column(Text)  # interview|rejected|no_feedback|offer
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FetchLog(Base):
    """§3.8 采集审计。"""

    __tablename__ = "fetch_log"
    __table_args__ = (Index("idx_fetch_log_company", "company_id", "started_at"),)

    id: Mapped[int] = mapped_column(BIGPK, primary_key=True, autoincrement=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    status: Mapped[str] = mapped_column(Text, nullable=False)  # success|failed|rate_limited|robots_blocked
    job_count: Mapped[int] = mapped_column(Integer, default=0)
    error_msg: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SkillTag(Base):
    """§3.9 标签字典：技能/工种归一化，canonical 为标准名。"""

    __tablename__ = "skill_tags"

    id: Mapped[int] = mapped_column(BIGPK, primary_key=True, autoincrement=True)
    canonical: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)  # skill|role
    aliases: Mapped[list] = mapped_column(JSONType, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
