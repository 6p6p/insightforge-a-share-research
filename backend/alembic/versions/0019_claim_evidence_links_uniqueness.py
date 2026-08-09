"""claim_evidence_links cross-relation uniqueness

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-09

阶段 4A final closeout：把 v1 domain 不变量"同一 EvidenceCard 对同一 Claim
只能存在一条 link（supports / contradicts / context 中恰好一种 relation）"
下沉到数据库层强制。

- PK 仍是 (claim_id, evidence_card_id, relation)，保持既有结构不变；
  0019 纯增量新增 `UNIQUE(claim_id, evidence_card_id)`
  （uq_claim_evidence_links_claim_evidence）：同 claim + 同 evidence 的
  跨 relation 重复插入由数据库直接拒绝（此前只在 ClaimDraft 构造时约束，
  DB 层允许直接 SQL 写入互相矛盾的 relation）。
- **不修改已落地的 0018**；0019 只加约束、不加列、不动数据。
- 对既有合法数据无副作用：v1 合法数据本来就满足每条 (claim, evidence)
  最多一条 link。

downgrade guard：`claim_evidence_links` 有行时显式拒绝回滚——删除该约束会
静默允许跨 relation 重复、改变 v1 语义；无数据时才允许回到 0018。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT_NAME = "uq_claim_evidence_links_claim_evidence"


def _table_has_row(table: str, where: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {table} WHERE {where} LIMIT 1"))
    return rows.first() is not None


def upgrade() -> None:
    op.create_unique_constraint(
        _CONSTRAINT_NAME,
        "claim_evidence_links",
        ["claim_id", "evidence_card_id"],
    )


def downgrade() -> None:
    # 数据安全：存在任何 relation 数据时拒绝回滚。删除该约束会静默允许同一
    # Claim 对同一 Evidence 出现互相矛盾的 relation（supports+contradicts），
    # 改变 v1 语义；不能为了移除约束而静默改变语义。
    if _table_has_row("claim_evidence_links", "1 = 1"):
        raise RuntimeError(
            "cannot downgrade migration 0019: claim_evidence_links rows present; "
            "refusing to drop cross-relation uniqueness silently"
        )
    op.drop_constraint(_CONSTRAINT_NAME, "claim_evidence_links", type_="unique")
