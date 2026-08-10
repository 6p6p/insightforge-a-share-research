"""macro_transmission_chains + macro_transmission_evidence_links

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-10

阶段 4C.1A Macro Transmission Provenance Foundation：持久化「Macro Evidence
+ Company Exposure Evidence → Macro Transmission Chain → Macro Claim」的传导
**分析产物**，使 Audit 可回溯"宏观变量如何传到公司"。

- **macro_transmission_chains**：一条传导链 = 一个 Macro Claim 专属、UNIQUE
  claim_id FK claims **CASCADE**（删 Claim 删链）；company_id FK companies
  **RESTRICT**；channel_type ∈ revenue/cost/financing/demand/supply_chain/
  trade_policy/operations/other（channel 描述宏观如何传到公司，不是宏观变量本身）；
  effect_direction ∈ tailwind/headwind/mixed/uncertain（不是 buy/sell）；
  impact_status ∈ plausible_impact/observed_impact；time_alignment ∈
  aligned/uncertain（**无 misaligned**：证据明确错位时 Service 拒绝而非存
  misaligned Claim）；transmission_schema_version >= 1；
  transmission_fingerprint CHAR(64) UNIQUE（变更 → 新链，旧链保留）。
- **macro_transmission_evidence_links**：transmission_id FK chains **CASCADE**；
  evidence_card_id FK evidence_cards **RESTRICT**（证据存在期间 link 不静默
  消失）；role ∈ macro_driver / company_exposure / observed_effect；
  PK(transmission_id, evidence_card_id, role)；**UNIQUE(transmission_id,
  evidence_card_id)**——同一证据对同一链只能一种 role；INDEX evidence_card_id。
- **Transmission 不是 EvidenceCard**：传导是分析产物（利率 → 融资渠道 → 有息
  负债 → 融资成本压力），禁止把传导链伪装成 EvidenceCard。
- downgrade guard：两个表任一行存在时拒绝回滚（不静默丢弃传导 provenance）；
  空数据时才允许回到 0022。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHAINS = "macro_transmission_chains"
_LINKS = "macro_transmission_evidence_links"

_ROLE_CHECK = "role IN ('macro_driver','company_exposure','observed_effect')"
_CHANNEL_TYPE_CHECK = (
    "channel_type IN ('revenue','cost','financing','demand','supply_chain',"
    "'trade_policy','operations','other')"
)
_EFFECT_DIRECTION_CHECK = "effect_direction IN ('tailwind','headwind','mixed','uncertain')"
_IMPACT_STATUS_CHECK = "impact_status IN ('plausible_impact','observed_impact')"
_TIME_ALIGNMENT_CHECK = "time_alignment IN ('aligned','uncertain')"


def _table_has_row(table: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1"))
    return rows.first() is not None


def upgrade() -> None:
    op.create_table(
        _CHAINS,
        sa.Column(
            "transmission_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "claim_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("claims.claim_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.company_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("channel_type", sa.String(32), nullable=False),
        sa.Column("effect_direction", sa.String(16), nullable=False),
        sa.Column("impact_status", sa.String(16), nullable=False),
        sa.Column("time_alignment", sa.String(16), nullable=False),
        sa.Column("transmission_schema_version", sa.Integer(), nullable=False),
        sa.Column("transmission_fingerprint", postgresql.CHAR(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "transmission_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_macro_transmission_chains_fingerprint",
        ),
        sa.CheckConstraint(
            "transmission_schema_version >= 1",
            name="ck_macro_transmission_chains_schema_version",
        ),
        sa.CheckConstraint(_CHANNEL_TYPE_CHECK, name="ck_macro_transmission_chains_channel_type"),
        sa.CheckConstraint(
            _EFFECT_DIRECTION_CHECK,
            name="ck_macro_transmission_chains_effect_direction",
        ),
        sa.CheckConstraint(
            _IMPACT_STATUS_CHECK,
            name="ck_macro_transmission_chains_impact_status",
        ),
        sa.CheckConstraint(
            _TIME_ALIGNMENT_CHECK,
            name="ck_macro_transmission_chains_time_alignment",
        ),
        sa.UniqueConstraint("claim_id", name="uq_macro_transmission_chains_claim_id"),
        sa.UniqueConstraint(
            "transmission_fingerprint",
            name="uq_macro_transmission_chains_transmission_fingerprint",
        ),
    )

    op.create_table(
        _LINKS,
        sa.Column(
            "transmission_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("macro_transmission_chains.transmission_id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "evidence_card_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("evidence_cards.evidence_card_id", ondelete="RESTRICT"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("role", sa.String(16), primary_key=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(_ROLE_CHECK, name="ck_macro_transmission_evidence_links_role"),
        sa.UniqueConstraint(
            "transmission_id",
            "evidence_card_id",
            name="uq_macro_transmission_evidence_links_transmission_evidence",
        ),
    )
    op.create_index(
        f"ix_{_LINKS}_evidence_card_id",
        _LINKS,
        ["evidence_card_id"],
    )


def downgrade() -> None:
    # 数据安全：存在任何 Macro Transmission provenance 时拒绝回滚，不静默丢弃。
    if _table_has_row(_CHAINS) or _table_has_row(_LINKS):
        raise RuntimeError(
            "cannot downgrade migration 0023: macro_transmission provenance rows present; "
            "refusing to drop macro transmission chains silently (alembic_version stays 0023)"
        )
    op.drop_index(f"ix_{_LINKS}_evidence_card_id", table_name=_LINKS)
    op.drop_table(_LINKS)
    op.drop_table(_CHAINS)
