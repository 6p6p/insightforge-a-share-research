"""chunk vector index manifests

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-08

阶段 3B.1：
- chunk_sets 增加自然身份 UNIQUE(parsed_source_id, chunker_name,
  chunker_version)：0014 只按 chunk_set_fingerprint 唯一，缺显式自然身份
  （同 ParsedSource + 同 chunker 身份 → 最多 1 个 ChunkSet）。
- 新建 chunk_vector_indexes：每个 (ChunkSet, embedding 模型配置) 一个
  **可重建向量索引 manifest**。PostgreSQL = Source of Truth，Chroma =
  derived index；该表只登记"哪个 ChunkSet、用哪个模型配置、期望/实际
  chunk 数、collection 名与 schema 版本、确定性指纹、状态"，不存 embedding。
  - 自然身份 UNIQUE(chunk_set_id, embedding_model_id, embedding_model_revision,
    collection_schema_version)：并发重建共享同一行（确定性 id + upsert 幂等）。
  - index_fingerprint 不含 timestamps / DB ID / status，只含可重建语义字段。
  - status: building → ready / failed；retry 复跑 failed/building。

downgrade 沿用 0013/0014 约定：存在任何 manifest 数据时拒绝回滚，不静默丢数据。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_has_row(table: str, where: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {table} WHERE {where} LIMIT 1"))
    return rows.first() is not None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_chunk_sets_identity",
        "chunk_sets",
        ["parsed_source_id", "chunker_name", "chunker_version"],
    )

    op.create_table(
        "chunk_vector_indexes",
        sa.Column("vector_index_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "chunk_set_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chunk_sets.chunk_set_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("embedding_model_id", sa.String(length=200), nullable=False),
        sa.Column("embedding_model_revision", sa.String(length=200), nullable=False),
        sa.Column("embedding_dimension", sa.Integer(), nullable=False),
        sa.Column("normalize_embeddings", sa.Boolean(), nullable=False),
        sa.Column("collection_name", sa.String(length=200), nullable=False),
        sa.Column("collection_schema_version", sa.Integer(), nullable=False),
        sa.Column("expected_chunk_count", sa.BigInteger(), nullable=False),
        sa.Column("indexed_chunk_count", sa.BigInteger(), nullable=False),
        sa.Column("index_fingerprint", postgresql.CHAR(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'building'"),
        ),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "index_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_chunk_vector_indexes_fingerprint",
        ),
        sa.CheckConstraint(
            "embedding_dimension > 0",
            name="ck_chunk_vector_indexes_dimension",
        ),
        sa.CheckConstraint(
            "expected_chunk_count >= 0",
            name="ck_chunk_vector_indexes_expected_count",
        ),
        sa.CheckConstraint(
            "indexed_chunk_count >= 0",
            name="ck_chunk_vector_indexes_indexed_count",
        ),
        sa.CheckConstraint(
            "indexed_chunk_count <= expected_chunk_count",
            name="ck_chunk_vector_indexes_indexed_lte_expected",
        ),
        sa.CheckConstraint(
            "status IN ('building','ready','failed')",
            name="ck_chunk_vector_indexes_status",
        ),
        sa.CheckConstraint(
            "btrim(embedding_model_id) <> ''",
            name="ck_chunk_vector_indexes_model_id_not_blank",
        ),
        sa.CheckConstraint(
            "btrim(embedding_model_revision) <> ''",
            name="ck_chunk_vector_indexes_model_revision_not_blank",
        ),
        sa.CheckConstraint(
            "btrim(collection_name) <> ''",
            name="ck_chunk_vector_indexes_collection_name_not_blank",
        ),
        sa.UniqueConstraint(
            "index_fingerprint",
            name="uq_chunk_vector_indexes_fingerprint",
        ),
        sa.UniqueConstraint(
            "chunk_set_id",
            "embedding_model_id",
            "embedding_model_revision",
            "collection_schema_version",
            name="uq_chunk_vector_indexes_identity",
        ),
    )
    op.create_index(
        "ix_chunk_vector_indexes_chunk_set_id",
        "chunk_vector_indexes",
        ["chunk_set_id"],
    )


def downgrade() -> None:
    # 数据安全：存在任何 manifest 时拒绝回滚，不静默丢弃向量索引登记。
    if _table_has_row("chunk_vector_indexes", "1=1"):
        raise RuntimeError(
            "cannot downgrade migration 0015: chunk_vector_indexes contains rows; "
            "refusing to drop vector index manifests silently"
        )
    op.drop_table("chunk_vector_indexes")
    op.drop_constraint("uq_chunk_sets_identity", "chunk_sets", type_="unique")
