"""claim_financial_calculation_links + financial_calculation_inputs UNIQUE

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-09

阶段 4B.2C.1 Financial Claim Provenance Foundation：持久化 Claim ↔
FinancialCalculation 链接（Claim → ClaimFinancialCalculationLink →
FinancialCalculation → FinancialMetricObservation → EvidenceCard → Source），
使 Audit 可确定性重算。**不把 FinancialCalculation 伪装成 EvidenceCard**：
Calculation = derived deterministic fact，EvidenceCard = source-backed fact，
保持分层。

- `claim_financial_calculation_links`：claim_id FK claims **CASCADE**（删 Claim
  删 links）；calculation_id FK financial_calculations **RESTRICT**（计算存在
  期间 link 不静默消失）；relation ∈ supports / contradicts / context；
  PK(claim_id, calculation_id, relation)；**UNIQUE(claim_id, calculation_id)**
  ——一个 Calculation 对同一 Claim 只能一种 relation；INDEX calculation_id。
- **Gate 0 C**：`financial_calculation_inputs` 增加
  `uq_financial_calculation_inputs_calc_observation`（UNIQUE(calculation_id,
  metric_observation_id)）——同一 calculation 内同一 Observation 只能绑定一个
  role，杜绝同源数值被重复当作两个输入（Draft/Service 层已先拒绝）。
- downgrade guard：claim_financial_calculation_links 有行时拒绝回滚（不静默
  丢弃 Claim ↔ Calculation 链接）；无数据时才允许回到 0021。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LINKS = "claim_financial_calculation_links"
_INPUTS = "financial_calculation_inputs"

_RELATION_CHECK = "relation IN ('supports','contradicts','context')"


def _table_has_row(table: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1"))
    return rows.first() is not None


def upgrade() -> None:
    # Gate 0 C：同一 calculation 内同一 Observation 只能绑定一个 role。
    op.create_unique_constraint(
        "uq_financial_calculation_inputs_calc_observation",
        _INPUTS,
        ["calculation_id", "metric_observation_id"],
    )

    op.create_table(
        _LINKS,
        sa.Column(
            "claim_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("claims.claim_id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "calculation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("financial_calculations.calculation_id", ondelete="RESTRICT"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("relation", sa.String(16), primary_key=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(_RELATION_CHECK, name="ck_claim_financial_calculation_links_relation"),
        sa.UniqueConstraint(
            "claim_id",
            "calculation_id",
            name="uq_claim_financial_calculation_links_claim_calculation",
        ),
    )
    op.create_index(
        f"ix_{_LINKS}_calculation_id",
        _LINKS,
        ["calculation_id"],
    )


def downgrade() -> None:
    # 数据安全：存在任何 Claim ↔ Calculation 链接数据时拒绝回滚，不静默丢弃。
    if _table_has_row(_LINKS):
        raise RuntimeError(
            "cannot downgrade migration 0022: claim_financial_calculation_links rows present; "
            "refusing to drop claim-calculation provenance silently"
        )
    op.drop_index(f"ix_{_LINKS}_calculation_id", table_name=_LINKS)
    op.drop_table(_LINKS)
    op.drop_constraint(
        "uq_financial_calculation_inputs_calc_observation",
        _INPUTS,
        type_="unique",
    )
