"""deterministic parsing

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-07

阶段 2E.1：
- 新建 parsed_sources：SourceRecord 的确定性解析快照（parser_name /
  parser_version / raw_content_sha256 / parse_fingerprint UNIQUE /
  extracted_title / extracted_published_at / block_count）。
- 新建 parsed_source_blocks：有序文本块（ordinal >= 1，同 ParsedSource
  内 UNIQUE）、block_type 五类 CHECK、text trim 非空、text_sha256、
  locator JSONB（DOM 定位，用于后续 Evidence 原文核对）。
- 通用解析模型：PDF 后续（2E.2）复用同一模型，只是 parser_name/locator
  不同；本阶段只实现 HTML 解析器（html_dom v1）。
- 两表均为新增（无既有表约束演进），downgrade 直接删表；若 parsed_sources
  已有数据则拒绝回滚（沿用 0012 的不静默丢数据约定）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BLOCK_TYPE_CHECK = "block_type IN ('heading','paragraph','list_item','blockquote','table_text')"


def _table_has_row(table: str, where: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {table} WHERE {where} LIMIT 1"))
    return rows.first() is not None


def upgrade() -> None:
    op.create_table(
        "parsed_sources",
        sa.Column("parsed_source_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("source_records.source_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "artifact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("raw_artifacts.artifact_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("parser_name", sa.String(length=64), nullable=False),
        sa.Column("parser_version", sa.BigInteger(), nullable=False),
        sa.Column("raw_content_sha256", postgresql.CHAR(length=64), nullable=False),
        sa.Column("parse_fingerprint", postgresql.CHAR(length=64), nullable=False),
        sa.Column("extracted_title", sa.Text(), nullable=True),
        sa.Column("extracted_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("block_count", sa.BigInteger(), nullable=False),
        sa.Column("parsed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "raw_content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_parsed_sources_raw_content_sha256",
        ),
        sa.CheckConstraint(
            "parse_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_parsed_sources_parse_fingerprint",
        ),
        sa.CheckConstraint(
            "btrim(parser_name) <> ''",
            name="ck_parsed_sources_parser_name_not_blank",
        ),
        sa.CheckConstraint(
            "parser_version >= 1",
            name="ck_parsed_sources_parser_version",
        ),
        sa.CheckConstraint(
            "block_count >= 0",
            name="ck_parsed_sources_block_count",
        ),
        sa.UniqueConstraint(
            "parse_fingerprint",
            name="uq_parsed_sources_parse_fingerprint",
        ),
    )
    op.create_index(
        "ix_parsed_sources_source_id",
        "parsed_sources",
        ["source_id"],
    )
    op.create_index(
        "ix_parsed_sources_artifact_id",
        "parsed_sources",
        ["artifact_id"],
    )

    op.create_table(
        "parsed_source_blocks",
        sa.Column("block_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "parsed_source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("parsed_sources.parsed_source_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.BigInteger(), nullable=False),
        sa.Column("block_type", sa.String(length=32), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_sha256", postgresql.CHAR(length=64), nullable=False),
        sa.Column("locator", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "ordinal >= 1",
            name="ck_parsed_source_blocks_ordinal",
        ),
        sa.CheckConstraint(
            "text_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_parsed_source_blocks_text_sha256",
        ),
        sa.CheckConstraint(
            "btrim(text) <> ''",
            name="ck_parsed_source_blocks_text_not_blank",
        ),
        sa.CheckConstraint(
            _BLOCK_TYPE_CHECK,
            name="ck_parsed_source_blocks_block_type",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(locator) = 'object'",
            name="ck_parsed_source_blocks_locator_object",
        ),
        sa.UniqueConstraint(
            "parsed_source_id",
            "ordinal",
            name="uq_parsed_source_blocks_source_ordinal",
        ),
    )


def downgrade() -> None:
    # 数据安全：存在任何解析快照时拒绝回滚，不静默丢弃解析数据。
    if _table_has_row("parsed_sources", "1=1"):
        raise RuntimeError(
            "cannot downgrade migration 0013: parsed_sources contains rows; "
            "refusing to drop parsed snapshots silently"
        )
    op.drop_table("parsed_source_blocks")
    op.drop_table("parsed_sources")
