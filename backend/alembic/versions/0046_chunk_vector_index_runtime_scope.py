"""chunk vector index runtime scope isolation

Revision ID: 0046
Revises: 0045
Create Date: 2026-08-13

阶段 7B.1.4C.1 修复：eval per-attempt derived-state race。

问题：不同 evaluation attempt（Attempt A / B）虽然 Chroma collection 不同，
却共享同一个 PG `chunk_vector_indexes` manifest 自然身份
（chunk_set_id, embedding_model_id, embedding_model_revision,
collection_schema_version）。Single RAG 用 `force_rebuild=True` 把同一
ChunkSet 重建进不同 attempt 的 collection，于是两个 attempt 交替 reset /
mark_ready **同一行** manifest——cross-attempt derived-state race。

本 migration 把 `runtime_scope` 加入自然身份：

- 新增 `runtime_scope` VARCHAR(128) NOT NULL，默认 `'production'`；
  **existing production rows backfill** 到 `'production'`（server default 生效），
  不删除任何历史行；
- 更新 UNIQUE 约束 `uq_chunk_vector_indexes_identity` 为五元组
  （含 runtime_scope）；
- 新增非空 CHECK `ck_chunk_vector_indexes_runtime_scope_not_blank`。

生产行为不变：production 调用方不传 runtime_scope → 默认 `'production'`，
自然身份四元组语义与之前一致。eval 每个 attempt 用
`eval:<variant>:<execution_id.hex>` → 各得自己的 manifest row，互不覆盖。

**downgrade guard**：任何 `runtime_scope != 'production'` 的行（即本 migration
引入的 eval manifest）存在 → 拒绝回滚（否则五元组身份退化为四元组会静默丢失
scope 区分）；全部为 `'production'`（或空表）时才允许回到 0045。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "chunk_vector_indexes"
_IDENTITY = "uq_chunk_vector_indexes_identity"
_NOT_BLANK = "ck_chunk_vector_indexes_runtime_scope_not_blank"


def _has_non_production_scope() -> bool:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(f"SELECT 1 FROM {_TABLE} WHERE runtime_scope IS DISTINCT FROM 'production' LIMIT 1")
    )
    return rows.first() is not None


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            "runtime_scope",
            sa.String(length=128),
            nullable=False,
            server_default="production",
        ),
    )
    # 约束与默认值对齐：非空 CHECK（btrim 非空），identity 五元组。
    op.create_check_constraint(_NOT_BLANK, _TABLE, "btrim(runtime_scope) <> ''")
    op.drop_constraint(_IDENTITY, _TABLE, type_="unique")
    op.create_unique_constraint(
        _IDENTITY,
        _TABLE,
        [
            "chunk_set_id",
            "embedding_model_id",
            "embedding_model_revision",
            "collection_schema_version",
            "runtime_scope",
        ],
    )


def downgrade() -> None:
    # 数据安全：存在任何非 production scope 的 manifest（eval attempt 隔离行）时
    # 拒绝回滚——去掉 runtime_scope 会让不同 attempt 的 manifest 退化成同一自然
    # 身份，静默丢失隔离语义。全部为 production（或空表）时才允许回到 0045。
    if _has_non_production_scope():
        raise RuntimeError(
            "cannot downgrade migration 0046: chunk_vector_indexes contains "
            "rows with runtime_scope != 'production'; refusing to drop "
            "runtime-scoped eval vector index manifests (alembic_version stays 0046)"
        )
    op.drop_constraint(_IDENTITY, _TABLE, type_="unique")
    op.create_unique_constraint(
        _IDENTITY,
        _TABLE,
        [
            "chunk_set_id",
            "embedding_model_id",
            "embedding_model_revision",
            "collection_schema_version",
        ],
    )
    op.drop_constraint(_NOT_BLANK, _TABLE, type_="check")
    op.drop_column(_TABLE, "runtime_scope")
