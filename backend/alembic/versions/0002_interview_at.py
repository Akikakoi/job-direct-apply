"""0002 applications.interview_at：面试时间登记（§12.6 P5 ② 面试陪伴闭环）。

Revision ID: 0002_interview_at
Revises: 0001_init
Create Date: 2026-09-24

说明：
- 只因面试陪伴的"定时催进"需要一个面试时间锚点而加列，可空；历史行不受影响；
- SQLite 与 PG 的 `ADD COLUMN ... NULL` 语义一致，无需数据回填；
- 存量库（dev 阶段 create_all 建的）可 `alembic stamp` 后手工执行本列，或直接
  依赖 create_all 补齐（开发期两者等价）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_interview_at"
down_revision = "0001_init"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("applications", sa.Column("interview_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("applications", "interview_at")