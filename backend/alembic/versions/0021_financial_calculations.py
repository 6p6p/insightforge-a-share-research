"""financial_calculations + financial_calculation_inputs

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-09

阶段 4B.2B：把**已登记 FinancialMetricObservation** 通过确定性公式计算为派生
财务事实（同比 / 环比 / margin / ratio），形成
Calculation → Observation → EvidenceCard → Source 证据链。

- `financial_calculations`：calculation_id UUID PK、company_id FK companies
  RESTRICT、calculation_code、result_value NUMERIC(38,12)、result_unit
  （cny / ratio，ratio 存 0.1234 而非 12.34）、calculation_schema_version、
  formula_version、calculation_fingerprint CHAR(64) UNIQUE、created_at。
- `financial_calculation_inputs`：calculation_id FK financial_calculations
  CASCADE、input_role、metric_observation_id FK financial_metric_observations
  RESTRICT；PK(calculation_id, input_role)，索引 metric_observation_id。
- 全程 Decimal（CALCULATION_SCALE=12、ROUND_HALF_EVEN）；result_value /
  result_unit / fingerprint 全部由 Service 从 draft 确定性派生，调用方不得提供。
- downgrade guard：两表任一有行时拒绝回滚——删除会静默丢弃已计算的财务派生
  事实；无数据时才允许回到 0020。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CALCULATIONS = "financial_calculations"
_INPUTS = "financial_calculation_inputs"

_CALCULATION_CODE_CHECK = (
    "calculation_code IN ("
    "'absolute_change_cny','yoy_growth_rate','qoq_growth_rate',"
    "'gross_margin','operating_margin','net_margin_parent','debt_to_assets_ratio')"
)
_INPUT_ROLE_CHECK = (
    "input_role IN ("
    "'current','baseline','revenue','operating_cost','operating_profit',"
    "'net_profit_parent','total_assets','total_liabilities')"
)


def _table_has_row(table: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1"))
    return rows.first() is not None


def upgrade() -> None:
    op.create_table(
        _CALCULATIONS,
        sa.Column(
            "calculation_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.company_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("calculation_code", sa.String(64), nullable=False),
        sa.Column("result_value", sa.Numeric(38, 12), nullable=False),
        sa.Column("result_unit", sa.String(16), nullable=False),
        sa.Column("calculation_schema_version", sa.Integer(), nullable=False),
        sa.Column("formula_version", sa.Integer(), nullable=False),
        sa.Column("calculation_fingerprint", postgresql.CHAR(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            _CALCULATION_CODE_CHECK,
            name="ck_financial_calculations_calculation_code",
        ),
        sa.CheckConstraint(
            "result_unit IN ('cny','ratio')",
            name="ck_financial_calculations_result_unit",
        ),
        sa.CheckConstraint(
            "calculation_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_financial_calculations_calculation_fingerprint",
        ),
        sa.CheckConstraint(
            "calculation_schema_version >= 1",
            name="ck_financial_calculations_calculation_schema_version",
        ),
        sa.CheckConstraint(
            "formula_version >= 1",
            name="ck_financial_calculations_formula_version",
        ),
        sa.CheckConstraint(
            "btrim(calculation_code) <> ''",
            name="ck_financial_calculations_calculation_code_not_blank",
        ),
        sa.UniqueConstraint(
            "calculation_fingerprint",
            name="uq_financial_calculations_calculation_fingerprint",
        ),
    )
    op.create_index(f"ix_{_CALCULATIONS}_company_id", _CALCULATIONS, ["company_id"])
    op.create_index(f"ix_{_CALCULATIONS}_calculation_code", _CALCULATIONS, ["calculation_code"])

    op.create_table(
        _INPUTS,
        sa.Column(
            "calculation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{_CALCULATIONS}.calculation_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("input_role", sa.String(32), nullable=False),
        sa.Column(
            "metric_observation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "financial_metric_observations.metric_observation_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "calculation_id", "input_role", name="pk_financial_calculation_inputs"
        ),
        sa.CheckConstraint(
            _INPUT_ROLE_CHECK,
            name="ck_financial_calculation_inputs_input_role",
        ),
        sa.CheckConstraint(
            "btrim(input_role) <> ''",
            name="ck_financial_calculation_inputs_input_role_not_blank",
        ),
    )
    op.create_index(f"ix_{_INPUTS}_metric_observation_id", _INPUTS, ["metric_observation_id"])


def downgrade() -> None:
    if _table_has_row(_CALCULATIONS) or _table_has_row(_INPUTS):
        raise RuntimeError(
            "cannot downgrade migration 0021: financial_calculations rows present; "
            "refusing to drop registered financial calculations"
        )
    op.drop_table(_INPUTS)
    op.drop_table(_CALCULATIONS)
