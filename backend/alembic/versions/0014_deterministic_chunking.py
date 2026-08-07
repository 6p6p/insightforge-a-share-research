"""deterministic chunking

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-08

阶段 3A：
- 新建 chunk_sets：ParsedSource 的确定性分块快照（chunker_name /
  chunker_version / source_parse_fingerprint / chunk_count /
  chunk_set_fingerprint UNIQUE）。
- 新建 document_chunks：有序文本块（ordinal >= 1，同 ChunkSet 内 UNIQUE）、
  text 非空、text_sha256、char_count、locator_refs JSONB 数组
  （每项：block_ordinal + char_start/char_end + 原 ParsedBlock locator）。
- 通用分块模型：PDF / HTML 复用同一模型，只是 locator_refs 内 locator 的
  type 不同（pdf_page / html_dom）。
- 两表均为新增（无既有表约束演进），downgrade 直接删表；若 chunk_sets
  已有数据则拒绝回滚（沿用 0013 的不静默丢数据约定）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_has_row(table: str, where: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {table} WHERE {where} LIMIT 1"))
    return rows.first() is not None


def upgrade() -> None:
    op.create_table(
        "chunk_sets",
        sa.Column("chunk_set_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "parsed_source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("parsed_sources.parsed_source_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("chunker_name", sa.String(length=64), nullable=False),
        sa.Column("chunker_version", sa.BigInteger(), nullable=False),
        sa.Column("source_parse_fingerprint", postgresql.CHAR(length=64), nullable=False),
        sa.Column("chunk_count", sa.BigInteger(), nullable=False),
        sa.Column("chunk_set_fingerprint", postgresql.CHAR(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source_parse_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_chunk_sets_source_parse_fingerprint",
        ),
        sa.CheckConstraint(
            "chunk_set_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_chunk_sets_chunk_set_fingerprint",
        ),
        sa.CheckConstraint(
            "btrim(chunker_name) <> ''",
            name="ck_chunk_sets_chunker_name_not_blank",
        ),
        sa.CheckConstraint(
            "chunker_version >= 1",
            name="ck_chunk_sets_chunker_version",
        ),
        sa.CheckConstraint(
            "chunk_count >= 0",
            name="ck_chunk_sets_chunk_count",
        ),
        sa.UniqueConstraint(
            "chunk_set_fingerprint",
            name="uq_chunk_sets_chunk_set_fingerprint",
        ),
    )
    op.create_index(
        "ix_chunk_sets_parsed_source_id",
        "chunk_sets",
        ["parsed_source_id"],
    )

    op.create_table(
        "document_chunks",
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "chunk_set_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chunk_sets.chunk_set_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_sha256", postgresql.CHAR(length=64), nullable=False),
        sa.Column("char_count", sa.BigInteger(), nullable=False),
        sa.Column("locator_refs", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "ordinal >= 1",
            name="ck_document_chunks_ordinal",
        ),
        sa.CheckConstraint(
            "text_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_document_chunks_text_sha256",
        ),
        sa.CheckConstraint(
            "char_count >= 1",
            name="ck_document_chunks_char_count",
        ),
        sa.CheckConstraint(
            "btrim(text) <> ''",
            name="ck_document_chunks_text_not_blank",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(locator_refs) = 'array'",
            name="ck_document_chunks_locator_refs_array",
        ),
        sa.UniqueConstraint(
            "chunk_set_id",
            "ordinal",
            name="uq_document_chunks_set_ordinal",
        ),
    )


def downgrade() -> None:
    # 数据安全：存在任何 ChunkSet 时拒绝回滚，不静默丢弃分块数据。
    if _table_has_row("chunk_sets", "1=1"):
        raise RuntimeError(
            "cannot downgrade migration 0014: chunk_sets contains rows; "
            "refusing to drop chunk sets silently"
        )
    op.drop_table("document_chunks")
    op.drop_table("chunk_sets")
