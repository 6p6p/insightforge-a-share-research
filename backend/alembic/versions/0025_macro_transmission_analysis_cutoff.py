"""macro_transmission_chains.analysis_as_of: persist analysis cutoff column

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-10

阶段 4C.1B（Gate 0）：`analysis_as_of` 目前只存在于 transmission / claim
fingerprint 与 MacroClaimDraft（语义输入），**不是查询列**——fingerprint 含日期
但 DB 无法从 claim_id 反推 cutoff；未来 Audit / Writer / API / Claim 检查必须能
从 claim_id 拿到 analysis_as_of。故新增查询列。

- **upgrade**：`macro_transmission_chains.analysis_as_of DATE NULL` +
  CHECK `(transmission_schema_version < 3 OR analysis_as_of IS NOT NULL)` +
  普通 INDEX `(company_id, analysis_as_of)`。**不做任何 backfill**：历史 v1/v2
  链保持 analysis_as_of=NULL（0024-era 语义），且**绝不从 created_at /
  published_at / reporting_period_end / fingerprint 反推历史 cutoff**。
- **downgrade**：仅当不存在任何当前 schema 数据时允许回滚——即（a）没有
  transmission_schema_version >= 3 的传导链 **且**（b）没有 claim_schema_version
  >= 6 的 macro Claim。存在任一 → 显式拒绝（不删除数据 / 不修改行 /
  不静默丢弃 cutoff provenance）。历史 v1/v4 或 v2/v5 数据不阻塞回滚。

不修改 0023 / 0024；不改写历史行。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHAINS = "macro_transmission_chains"
_CHECK_NAME = "ck_macro_transmission_chains_analysis_as_of_present"
_INDEX_NAME = "ix_macro_transmission_chains_company_analysis_as_of"


def _has_v3_transmission() -> bool:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT 1 FROM macro_transmission_chains WHERE transmission_schema_version >= 3 LIMIT 1"
        )
    )
    return rows.first() is not None


def _has_v6_macro_claim() -> bool:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT 1 FROM claims "
            "WHERE analysis_domain = 'macro' AND claim_schema_version >= 6 LIMIT 1"
        )
    )
    return rows.first() is not None


def upgrade() -> None:
    op.add_column(
        _CHAINS,
        sa.Column("analysis_as_of", sa.Date(), nullable=True),
    )
    op.create_check_constraint(
        _CHECK_NAME,
        _CHAINS,
        "transmission_schema_version < 3 OR analysis_as_of IS NOT NULL",
    )
    op.create_index(_INDEX_NAME, _CHAINS, ["company_id", "analysis_as_of"])


def downgrade() -> None:
    # 数据安全：存在 v3 链 / v6 macro Claim 时拒绝回滚（cutoff 是 v6 语义的一部分，
    # 静默丢弃会让 Audit 无法从 claim_id 追溯 analysis_as_of）。不删除数据、不修改行。
    if _has_v3_transmission() or _has_v6_macro_claim():
        raise RuntimeError(
            "cannot downgrade migration 0025: v3 transmission / v6 macro claim rows present; "
            "refusing to drop analysis_as_of cutoff column silently "
            "(alembic_version stays 0025)"
        )
    op.drop_index(_INDEX_NAME, table_name=_CHAINS)
    op.drop_constraint(_CHECK_NAME, _CHAINS, type_="check")
    op.drop_column(_CHAINS, "analysis_as_of")
