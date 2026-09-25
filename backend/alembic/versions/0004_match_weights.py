"""0004 匹配权重版本表：反馈回灌闭环（§12.5 收尾）。

Revision ID: 0004_match_weights
Revises: 0003_apply_consent
Create Date: 2026-09-25

说明：
- 反馈（`feedback_log`）→ 调参（`tuning.search_weights`）→ 采纳，需要一个**可审计、可回滚**的
  落地处；写 `.env` 等于让调参去改宿主机部署配置，既不可审计也不可回滚，故新建本表：
  每次采纳一行，生效权重 = 最新一行，表空时回退 `settings.MATCH_W_*`；
- 新增表而非改旧表：存量库只需建表（`alembic upgrade head`），无数据回填；
- 开发期 SQLite 靠 `Base.metadata.create_all` 也能补齐，但正式路径走本迁移。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_match_weights"
down_revision = "0003_apply_consent"
branch_labels = None
depends_on = None
BIGPK = sa.Integer().with_variant(sa.BigInteger(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "match_weight_versions",
        sa.Column("id", BIGPK, primary_key=True, autoincrement=True),
        sa.Column("weights", sa.JSON(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("sample_size", sa.Integer(), nullable=True),
        sa.Column("baseline_ndcg", sa.Float(), nullable=True),
        sa.Column("new_ndcg", sa.Float(), nullable=True),
        sa.Column("rematched", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("match_weight_versions")
