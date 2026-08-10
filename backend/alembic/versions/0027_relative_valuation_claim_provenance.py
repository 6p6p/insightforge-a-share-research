"""relative valuation claim provenance links + claim profiles

Revision ID: 0027
Revises: 0026
Create Date: 2026-08-10

阶段 4C.2B.1：Relative Valuation Claim Provenance Foundation。

两张表把 Relative Valuation Claim 与**已登记的 RelativeValuationComparison**
链接起来，形成 Claim → ClaimRelativeValuationComparisonLink →
RelativeValuationComparison → ValuationMetricObservation → EvidenceCard →
Source 完整可重算证据链（Audit 能重算 peer median / premium 并知道 judgment
基于哪些 peer comparisons）：

1. `claim_relative_valuation_comparison_links`——claim ↔ comparison 的**显式
   分析关系**（supports / contradicts / context，spec M：Comparison 承担相对
   claim 的分析关系；程序**不**根据 premium 自动决定 relation）。claim FK
   CASCADE；comparison FK RESTRICT（已登记的 comparison 不会被 Claim 删除连带
   清掉）；PK(claim_id, comparison_id, relation) + UNIQUE(claim_id,
   comparison_id)（同一 comparison 对同一 claim 只有一种 relation）+ INDEX
   comparison_id；CHECK relation IN ('supports','contradicts','context')。
2. `relative_valuation_claim_profiles`——claim 的 assessment 结构化视图
   （claim_id PK + FK claims CASCADE）。assessment v1（relative_high /
   broadly_in_line / relative_low / mixed / uncertain；**分析判断，不是公式
   输出**，**不做** buy/sell/bullish/bearish/cheap/expensive）；CHECK
   assessment + profile_schema_version >= 1。

**downgrade guard（spec V）**：`relative_valuation_claim_profiles` 或
`claim_relative_valuation_comparison_links` 任一存在行 → 拒绝回滚（删除表会
静默丢弃已登记的 Claim↔Comparison provenance 与 assessment 视图）；
alembic_version 保持 0027。全部为空时才允许回到 0026。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LINKS = "claim_relative_valuation_comparison_links"
_PROFILES = "relative_valuation_claim_profiles"

_RELATION_CHECK = "relation IN ('supports','contradicts','context')"
_ASSESSMENT_CHECK = (
    "assessment IN ('relative_high','broadly_in_line','relative_low','mixed','uncertain')"
)

_ALL_TABLES = (_PROFILES, _LINKS)


def _table_has_row(table: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1"))
    return rows.first() is not None


def upgrade() -> None:
    op.create_table(
        _LINKS,
        sa.Column(
            "claim_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("claims.claim_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "comparison_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "relative_valuation_comparisons.comparison_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("relation", sa.String(16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "claim_id",
            "comparison_id",
            "relation",
            name="pk_claim_relative_valuation_comparison_links",
        ),
        sa.UniqueConstraint(
            "claim_id",
            "comparison_id",
            name="uq_claim_relative_valuation_comparison_links_claim_comparison",
        ),
        sa.CheckConstraint(
            _RELATION_CHECK,
            name="ck_claim_relative_valuation_comparison_links_relation",
        ),
    )
    op.create_index(
        f"ix_{_LINKS}_comparison_id",
        _LINKS,
        ["comparison_id"],
    )

    op.create_table(
        _PROFILES,
        sa.Column(
            "claim_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("claims.claim_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("assessment", sa.String(32), nullable=False),
        sa.Column("analysis_as_of", sa.Date(), nullable=False),
        sa.Column("profile_schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "claim_id",
            name="pk_relative_valuation_claim_profiles",
        ),
        sa.CheckConstraint(
            _ASSESSMENT_CHECK,
            name="ck_relative_valuation_claim_profiles_assessment",
        ),
        sa.CheckConstraint(
            "profile_schema_version >= 1",
            name="ck_relative_valuation_claim_profiles_schema_version",
        ),
    )


def downgrade() -> None:
    # 数据安全（spec V）：profile 或 link 任一存在行 → 拒绝回滚（不删除数据 /
    # 不修改行 / 不静默丢弃 Claim↔Comparison provenance 与 assessment 视图），
    # alembic_version 保持 0027。全部为空时才允许回到 0026。
    for table in _ALL_TABLES:
        if _table_has_row(table):
            raise RuntimeError(
                "cannot downgrade migration 0027: "
                f"rows present in {table}; refusing to drop registered relative "
                "valuation claim profiles / comparison links "
                "(alembic_version stays 0027)"
            )
    op.drop_table(_PROFILES)
    op.drop_table(_LINKS)
