"""0005 match_scores 分数精度：Numeric(5,2) → Numeric(6,4)（并列的真根因）。

Revision ID: 0005_score_precision
Revises: 0004_match_weights
Create Date: 2026-09-26

说明：
- 现象：海外 top10 门槛上并列近百条（1918 条里最大同分块 855 条），排序实际退化成
  "谁先入库"（`updated_at`）。第二十五/二十六轮的 L3 role 分档与 L1+L2 权重归一确实
  提升了 rule 的区分度（σ +57%），但**并列几乎没动**——因为分数落库只有两位小数，
  `final_score` 被量化成 0.01 的整数倍，全库只剩 ~39 个不同取值。
- 打分侧本来就是 `round(..., 4)`（`match.weighted_rule` / `compute_match` 融合项），
  两位小数是**存储**在丢精度，不是算错。故只改列宽，不改任何打分逻辑。
- 四位小数并非"永远消除并列"（vec=0 且 rule 相同者仍会同分），但能把真实区分度
  落到库里；剩余并列由 L4 并列打破键（role/skill/词面重合）兜底。
- **必须全量重算 match_scores**：存量行的 vec 分已被旧列宽截断，改列后不会自动恢复。
- 只加宽不缩窄，无数据回填风险；SQLite（开发期 create_all）同样支持该列型。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_score_precision"
down_revision = "0004_match_weights"
branch_labels = None
depends_on = None

SCORE_COLUMNS = ("rule_score", "vec_score", "llm_score", "final_score")


def upgrade() -> None:
    for col in SCORE_COLUMNS:
        op.alter_column(
            "match_scores",
            col,
            existing_type=sa.Numeric(5, 2),
            type_=sa.Numeric(6, 4),
            existing_nullable=True,
        )


def downgrade() -> None:
    # 回退会按 0.01 舍入存量值（不可逆丢精度），回退后同样需要全量重算。
    for col in SCORE_COLUMNS:
        op.alter_column(
            "match_scores",
            col,
            existing_type=sa.Numeric(6, 4),
            type_=sa.Numeric(5, 2),
            existing_nullable=True,
        )
