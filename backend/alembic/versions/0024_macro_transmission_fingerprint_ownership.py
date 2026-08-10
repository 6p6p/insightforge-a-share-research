"""macro_transmission_chains.transmission_fingerprint: drop global UNIQUE

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-10

阶段 4C.1A Final Closeout：修正 transmission fingerprint 的 identity 语义。

- **upgrade**：删除 `uq_macro_transmission_chains_transmission_fingerprint`（0023
  引入的 global UNIQUE），改为普通索引
  `ix_macro_transmission_chains_transmission_fingerprint`。原因：相同 transmission
  semantics + 不同的 claim statement / analyst_version 必须允许 **new Claim + new
  MacroTransmissionChain**（fingerprint 可相同），old 保留。identity 由
  `claims.claim_fingerprint` UNIQUE 负责；`macro_transmission_chains.claim_id`
  UNIQUE 保证"一个 Macro Claim 拥有一条链"。
- **downgrade**：仅当不存在（a）任何 transmission_schema_version >= 2 的传导链、
  （b）任何 claim_schema_version >= 5 的 macro Claim、（c）任何重复
  transmission_fingerprint 时，才允许恢复 UNIQUE 回到 0023；否则显式拒绝
  （不删除数据 / 不修改 fingerprint / 不静默合并链）。

不引入 transmission ↔ claim many-to-many join table；不改写历史 v1/v4 行。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHAINS = "macro_transmission_chains"
_UNIQUE_NAME = "uq_macro_transmission_chains_transmission_fingerprint"
_INDEX_NAME = "ix_macro_transmission_chains_transmission_fingerprint"


def _has_v2_transmission() -> bool:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT 1 FROM macro_transmission_chains WHERE transmission_schema_version >= 2 LIMIT 1"
        )
    )
    return rows.first() is not None


def _has_v5_macro_claim() -> bool:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT 1 FROM claims "
            "WHERE analysis_domain = 'macro' AND claim_schema_version >= 5 LIMIT 1"
        )
    )
    return rows.first() is not None


def _has_duplicate_fingerprint() -> bool:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT 1 FROM macro_transmission_chains "
            "GROUP BY transmission_fingerprint HAVING count(*) > 1 LIMIT 1"
        )
    )
    return rows.first() is not None


def upgrade() -> None:
    op.drop_constraint(_UNIQUE_NAME, _CHAINS, type_="unique")
    op.create_index(_INDEX_NAME, _CHAINS, ["transmission_fingerprint"])


def downgrade() -> None:
    # 数据安全：存在 v2 链 / v5 macro Claim / 重复 fingerprint 时拒绝回滚，
    # 不删除数据、不修改 fingerprint、不静默合并链。
    if _has_v2_transmission() or _has_v5_macro_claim() or _has_duplicate_fingerprint():
        raise RuntimeError(
            "cannot downgrade migration 0024: v2 transmission / v5 macro claim / "
            "duplicate transmission_fingerprint rows present; refusing to restore "
            "transmission_fingerprint UNIQUE silently (alembic_version stays 0024)"
        )
    op.drop_index(_INDEX_NAME, table_name=_CHAINS)
    op.create_unique_constraint(_UNIQUE_NAME, _CHAINS, ["transmission_fingerprint"])
