"""0003 applications 同意留痕：authorized_at + consent_version（§14 ② 投递授权钩子）。

Revision ID: 0003_apply_consent
Revises: 0002_interview_at
Create Date: 2026-09-24

说明：
- 原有 `authorized` 只有布尔，无法回答"用户同意的是哪一版政策、什么时候同意的"——
  上线合规要求同意可核验，故加时间戳与政策版本两列（均取 `app/services/legal.py` 的
  POLICY_VERSION / POLICY_EFFECTIVE_DATE，前端页脚同源）；
- 两列可空：历史行（0003 之前的投递）没有留痕，语义上如实为"未知"，不回填假数据；
- SQLite 与 PG 的 `ADD COLUMN ... NULL` 语义一致，无需数据回填；存量库可
  依赖 create_all 补齐（开发期）或手工执行本列后再 `alembic stamp head`。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_apply_consent"
down_revision = "0002_interview_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("applications", sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("applications", sa.Column("consent_version", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("applications", "consent_version")
    op.drop_column("applications", "authorized_at")