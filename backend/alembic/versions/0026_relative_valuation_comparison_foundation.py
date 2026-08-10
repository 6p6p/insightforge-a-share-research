"""valuation metric observations + relative valuation comparisons + peers

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-10

阶段 4C.2A：Relative Valuation Data & Comparison Foundation。

三张表：
1. `valuation_metric_observations`——来源于真实文档 Evidence（document_chunk +
   evidence_type=metric）的**原始估值倍数数值事实**（pe_ttm / pb_mrq / ps_ttm），
   `metric_as_of` = 市场观测日（**不是**来源发布时间）；
2. `relative_valuation_comparisons`——对显式 peer 集合的**确定性派生比较事实**
   （comparison_method='peer_median'：peer_median / peer_min / peer_max /
   premium_discount_to_median = (target_value - peer_median)/peer_median），
   `analysis_as_of >= metric_as_of`（no-lookahead CHECK）；
3. `relative_valuation_comparison_peers`——comparison ↔ peer company /
   peer observation 链接（comparison FK CASCADE；company / observation FK
   RESTRICT；PK(comparison_id, peer_company_id)；
   UNIQUE(comparison_id, peer_observation_id)）。

所有 fingerprint CHAR(64) UNIQUE（并发最终 1 对象）；所有数值 NUMERIC(38,12)
Decimal 无失真落库；不复制 evidence locator_refs（provenance 经 observation →
EvidenceCard → Source 链接）。

**downgrade guard**：三张 valuation 表任一存在行 → 拒绝回滚（删除表会静默丢弃
已登记的估值观察 / 比较 / peer 链接 provenance）；全部为空时才允许回到 0025。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OBSERVATIONS = "valuation_metric_observations"
_COMPARISONS = "relative_valuation_comparisons"
_PEERS = "relative_valuation_comparison_peers"

_METRIC_CODE_CHECK = "metric_code IN ('pe_ttm','pb_mrq','ps_ttm')"
_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"

_ALL_TABLES = (_OBSERVATIONS, _COMPARISONS, _PEERS)


def _table_has_row(table: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1"))
    return rows.first() is not None


def upgrade() -> None:
    op.create_table(
        _OBSERVATIONS,
        sa.Column(
            "valuation_observation_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.company_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "source_evidence_card_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("evidence_cards.evidence_card_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("metric_code", sa.String(16), nullable=False),
        sa.Column("metric_as_of", sa.Date(), nullable=False),
        sa.Column("source_value_text", sa.Text(), nullable=False),
        sa.Column("metric_value", sa.Numeric(38, 12), nullable=False),
        sa.Column("valuation_observation_schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "valuation_observation_fingerprint",
            postgresql.CHAR(64),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            _METRIC_CODE_CHECK,
            name="ck_valuation_metric_observations_metric_code",
        ),
        sa.CheckConstraint(
            f"valuation_observation_fingerprint {_SHA256_CHECK}",
            name="ck_valuation_metric_observations_fingerprint",
        ),
        sa.CheckConstraint(
            "valuation_observation_schema_version >= 1",
            name="ck_valuation_metric_observations_schema_version",
        ),
        sa.CheckConstraint(
            "btrim(source_value_text) <> ''",
            name="ck_valuation_metric_observations_source_value_text_not_blank",
        ),
        sa.UniqueConstraint(
            "valuation_observation_fingerprint",
            name="uq_valuation_metric_observations_fingerprint",
        ),
    )
    op.create_index(f"ix_{_OBSERVATIONS}_company_id", _OBSERVATIONS, ["company_id"])
    op.create_index(
        f"ix_{_OBSERVATIONS}_source_evidence_card_id",
        _OBSERVATIONS,
        ["source_evidence_card_id"],
    )
    op.create_index(f"ix_{_OBSERVATIONS}_metric_code", _OBSERVATIONS, ["metric_code"])
    op.create_index(f"ix_{_OBSERVATIONS}_metric_as_of", _OBSERVATIONS, ["metric_as_of"])

    op.create_table(
        _COMPARISONS,
        sa.Column(
            "comparison_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "target_company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.company_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "target_observation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "valuation_metric_observations.valuation_observation_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("metric_code", sa.String(16), nullable=False),
        sa.Column("metric_as_of", sa.Date(), nullable=False),
        sa.Column("analysis_as_of", sa.Date(), nullable=False),
        sa.Column("comparison_method", sa.String(32), nullable=False),
        sa.Column("peer_count", sa.Integer(), nullable=False),
        sa.Column("peer_median", sa.Numeric(38, 12), nullable=False),
        sa.Column("peer_min", sa.Numeric(38, 12), nullable=False),
        sa.Column("peer_max", sa.Numeric(38, 12), nullable=False),
        sa.Column("premium_discount_to_median", sa.Numeric(38, 12), nullable=False),
        sa.Column("comparison_schema_version", sa.Integer(), nullable=False),
        sa.Column("formula_version", sa.Integer(), nullable=False),
        sa.Column("comparison_fingerprint", postgresql.CHAR(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            _METRIC_CODE_CHECK,
            name="ck_relative_valuation_comparisons_metric_code",
        ),
        sa.CheckConstraint(
            "comparison_method IN ('peer_median')",
            name="ck_relative_valuation_comparisons_method",
        ),
        sa.CheckConstraint(
            "peer_count BETWEEN 3 AND 20",
            name="ck_relative_valuation_comparisons_peer_count",
        ),
        sa.CheckConstraint(
            "analysis_as_of >= metric_as_of",
            name="ck_relative_valuation_comparisons_no_lookahead",
        ),
        sa.CheckConstraint(
            "formula_version >= 1",
            name="ck_relative_valuation_comparisons_formula_version",
        ),
        sa.CheckConstraint(
            "comparison_schema_version >= 1",
            name="ck_relative_valuation_comparisons_schema_version",
        ),
        sa.CheckConstraint(
            f"comparison_fingerprint {_SHA256_CHECK}",
            name="ck_relative_valuation_comparisons_fingerprint",
        ),
        sa.UniqueConstraint(
            "comparison_fingerprint",
            name="uq_relative_valuation_comparisons_fingerprint",
        ),
    )
    op.create_index(
        f"ix_{_COMPARISONS}_target_company_id",
        _COMPARISONS,
        ["target_company_id"],
    )

    op.create_table(
        _PEERS,
        sa.Column(
            "comparison_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "relative_valuation_comparisons.comparison_id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column(
            "peer_company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.company_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "peer_observation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "valuation_metric_observations.valuation_observation_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "comparison_id",
            "peer_company_id",
            name="pk_relative_valuation_comparison_peers",
        ),
        sa.UniqueConstraint(
            "comparison_id",
            "peer_observation_id",
            name="uq_relative_valuation_comparison_peers_observation",
        ),
    )
    op.create_index(
        f"ix_{_PEERS}_peer_observation_id",
        _PEERS,
        ["peer_observation_id"],
    )


def downgrade() -> None:
    # 数据安全：任一 valuation 表存在行 → 拒绝回滚（不删除数据 / 不修改行 /
    # 不静默丢弃估值观察 / 比较 / peer provenance），alembic_version 保持 0026。
    for table in _ALL_TABLES:
        if _table_has_row(table):
            raise RuntimeError(
                "cannot downgrade migration 0026: "
                f"rows present in {table}; refusing to drop registered valuation "
                "observations / comparisons / peer links "
                "(alembic_version stays 0026)"
            )
    op.drop_table(_PEERS)
    op.drop_table(_COMPARISONS)
    op.drop_table(_OBSERVATIONS)
