"""0001 初始迁移：按 app.models 元数据建全部 9 张表（含 resumes.is_active、jobs.city_keys）。

Revision ID: 0001_init
Revises:
Create Date: 2026-09-19

说明：
- 表结构以 ORM 元数据为唯一事实源（Base.metadata），避免手写 9 张表漂移；
- 已存在的库（dev 阶段用 create_all 建的）执行 `alembic stamp head` 打点即可；
- 后续加列：`alembic revision --autogenerate -m "..."` 生成增量迁移。
"""

from __future__ import annotations

from alembic import op

revision = "0001_init"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    from app.core.db import Base
    import app.models  # noqa: F401  注册全部模型

    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    from app.core.db import Base
    import app.models  # noqa: F401  注册全部模型

    Base.metadata.drop_all(bind=op.get_bind())
