"""financial_metric_observations

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-09

阶段 4B.2A：把来源于真实财务 Evidence 的**原始财务数值**登记为确定性的
`FinancialMetricObservation`，供后续（4B.2B）确定性财务计算。

- provenance：通过 `source_evidence_card_id`（FK evidence_cards RESTRICT）
  回到 EvidenceCard → quote → DocumentChunk → ParsedSource → SourceRecord →
  RawArtifact → locator；**不复制 locator_refs** 到本表。
- raw_value 完全由 source_value_text 解析得到；normalized_value_cny =
  raw_value × raw_unit 系数（全 Decimal，无 float）。
- metric_fingerprint UNIQUE（64 hex CHAR）+ CK 约束（statement_scope /
  period_kind / raw_unit / period 一致性 / fingerprint 格式 / schema >= 1 /
  非空）。
- 四个查询索引：company_id / source_evidence_card_id / metric_code / period_end。
- downgrade guard：`financial_metric_observations` 有行时拒绝回滚——删除表会
  静默丢弃已登记数值事实；无数据时才允许回到 0019。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "financial_metric_observations"


def _table_has_row(table: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1"))
    return rows.first() is not None


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column(
            "metric_observation_id",
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
        sa.Column("metric_code", sa.String(64), nullable=False),
        sa.Column("statement_scope", sa.String(16), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("period_kind", sa.String(16), nullable=False),
        sa.Column("source_value_text", sa.Text(), nullable=False),
        sa.Column("raw_value", sa.Numeric(38, 12), nullable=False),
        sa.Column("raw_unit", sa.String(32), nullable=False),
        sa.Column("normalized_value_cny", sa.Numeric(38, 12), nullable=False),
        sa.Column("metric_schema_version", sa.Integer(), nullable=False),
        sa.Column("metric_fingerprint", postgresql.CHAR(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "statement_scope IN ('consolidated','parent')",
            name="ck_financial_metric_observations_statement_scope",
        ),
        sa.CheckConstraint(
            "period_kind IN ('duration','instant')",
            name="ck_financial_metric_observations_period_kind",
        ),
        sa.CheckConstraint(
            "raw_unit IN ('yuan','thousand_yuan','ten_thousand_yuan','hundred_million_yuan')",
            name="ck_financial_metric_observations_raw_unit",
        ),
        sa.CheckConstraint(
            "(period_kind = 'instant' AND period_start IS NULL) OR "
            "(period_kind = 'duration' AND period_start IS NOT NULL "
            "AND period_start <= period_end)",
            name="ck_financial_metric_observations_period_consistency",
        ),
        sa.CheckConstraint(
            "metric_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_financial_metric_observations_metric_fingerprint",
        ),
        sa.CheckConstraint(
            "metric_schema_version >= 1",
            name="ck_financial_metric_observations_metric_schema_version",
        ),
        sa.CheckConstraint(
            "btrim(metric_code) <> ''",
            name="ck_financial_metric_observations_metric_code_not_blank",
        ),
        sa.CheckConstraint(
            "btrim(source_value_text) <> ''",
            name="ck_financial_metric_observations_source_value_text_not_blank",
        ),
        sa.UniqueConstraint(
            "metric_fingerprint",
            name="uq_financial_metric_observations_metric_fingerprint",
        ),
    )
    op.create_index(f"ix_{_TABLE}_company_id", _TABLE, ["company_id"])
    op.create_index(f"ix_{_TABLE}_source_evidence_card_id", _TABLE, ["source_evidence_card_id"])
    op.create_index(f"ix_{_TABLE}_metric_code", _TABLE, ["metric_code"])
    op.create_index(f"ix_{_TABLE}_period_end", _TABLE, ["period_end"])


def downgrade() -> None:
    if _table_has_row(_TABLE):
        raise RuntimeError(
            "cannot downgrade migration 0020: financial_metric_observations rows "
            "present; refusing to drop registered financial metric observations"
        )
    op.drop_table(_TABLE)
