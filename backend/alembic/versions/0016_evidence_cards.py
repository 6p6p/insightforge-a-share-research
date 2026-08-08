"""evidence cards

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-09

阶段 3C.1：确定性登记"已确认的 DocumentChunk 原文片段"为可追溯的
EvidenceCard（Source → Evidence → Claim → Report → Audit 证据链中
**Evidence 的最小原子单元**）。

设计要点：
- 证据链 provenance 全量快照落库：company / source / parsed_source /
  chunk_set / chunk 五个 FK 全部 RESTRICT（上游存在期间 EvidenceCard
  不会被级联删除），provider_key / source_published_at /
  reporting_period_end / authority_tier_snapshot /
  critical_claim_eligible_snapshot 由 Service 从真实 provenance 派生
  （调用方不可提供）。
- quote 必须精确切片自 chunk.text[quote_start:quote_end]（程序生成，
  不信任调用方 / LLM）：quote_start >= 0、quote_end > quote_start、
  quote_text trim 后非空、quote_sha256 64 位小写 hex。
- locator_refs 是 **quote 级**投影（project_evidence_locator_refs 输出），
  不是整 chunk 的 locator_refs：char 范围缩窄到原 ParsedBlock 对应区间。
- evidence_fingerprint = canonical JSON + SHA-256（覆盖证据语义 + 原文
  切片 + provenance + extractor 配置），UNIQUE：同一完全相同证据 →
  replay 同一卡，并发最终只 1 张；语义 / quote / extractor version 任一
  变化 → 新指纹 → 新卡，旧卡保留（修订 = 新 EvidenceCard，无 update API）。
- evidence_schema_version >= 1；extractor_version >= 1；
  extractor_confidence IN (low, medium, high)；
  evidence_type IN (fact, metric, event, statement, context)。

downgrade 沿用 0013/0014/0015 约定：存在任何 EvidenceCard 数据时拒绝回滚，
不静默丢弃证据。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"
_EVIDENCE_TYPE_CHECK = "evidence_type IN ('fact','metric','event','statement','context')"
_CONFIDENCE_CHECK = "extractor_confidence IN ('low','medium','high')"


def _table_has_row(table: str, where: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {table} WHERE {where} LIMIT 1"))
    return rows.first() is not None


def upgrade() -> None:
    op.create_table(
        "evidence_cards",
        sa.Column("evidence_card_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.company_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("source_records.source_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "parsed_source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("parsed_sources.parsed_source_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "chunk_set_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chunk_sets.chunk_set_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "chunk_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("document_chunks.chunk_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("research_question", sa.Text(), nullable=False),
        sa.Column("research_question_sha256", postgresql.CHAR(length=64), nullable=False),
        sa.Column("evidence_statement", sa.Text(), nullable=False),
        sa.Column("evidence_type", sa.String(length=16), nullable=False),
        sa.Column("quote_start", sa.BigInteger(), nullable=False),
        sa.Column("quote_end", sa.BigInteger(), nullable=False),
        sa.Column("quote_text", sa.Text(), nullable=False),
        sa.Column("quote_sha256", postgresql.CHAR(length=64), nullable=False),
        sa.Column("locator_refs", postgresql.JSONB(), nullable=False),
        sa.Column(
            "provider_key",
            sa.String(length=32),
            sa.ForeignKey("source_providers.provider_key", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reporting_period_end", sa.Date(), nullable=True),
        sa.Column("authority_tier_snapshot", sa.SmallInteger(), nullable=False),
        sa.Column("critical_claim_eligible_snapshot", sa.Boolean(), nullable=False),
        sa.Column("extractor_name", sa.String(length=64), nullable=False),
        sa.Column("extractor_version", sa.BigInteger(), nullable=False),
        sa.Column("extractor_model_id", sa.String(length=200), nullable=True),
        sa.Column("extractor_confidence", sa.String(length=16), nullable=False),
        sa.Column("evidence_schema_version", sa.Integer(), nullable=False),
        sa.Column("evidence_fingerprint", postgresql.CHAR(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "quote_start >= 0",
            name="ck_evidence_cards_quote_start",
        ),
        sa.CheckConstraint(
            "quote_end > quote_start",
            name="ck_evidence_cards_quote_end",
        ),
        sa.CheckConstraint(
            _EVIDENCE_TYPE_CHECK,
            name="ck_evidence_cards_evidence_type",
        ),
        sa.CheckConstraint(
            _CONFIDENCE_CHECK,
            name="ck_evidence_cards_extractor_confidence",
        ),
        sa.CheckConstraint(
            "extractor_version >= 1",
            name="ck_evidence_cards_extractor_version",
        ),
        sa.CheckConstraint(
            "evidence_schema_version >= 1",
            name="ck_evidence_cards_evidence_schema_version",
        ),
        sa.CheckConstraint(
            f"research_question_sha256 {_SHA256_CHECK}",
            name="ck_evidence_cards_research_question_sha256",
        ),
        sa.CheckConstraint(
            f"quote_sha256 {_SHA256_CHECK}",
            name="ck_evidence_cards_quote_sha256",
        ),
        sa.CheckConstraint(
            f"evidence_fingerprint {_SHA256_CHECK}",
            name="ck_evidence_cards_evidence_fingerprint",
        ),
        sa.CheckConstraint(
            "authority_tier_snapshot BETWEEN 1 AND 4",
            name="ck_evidence_cards_authority_tier_snapshot",
        ),
        sa.CheckConstraint(
            "btrim(research_question) <> ''",
            name="ck_evidence_cards_research_question_not_blank",
        ),
        sa.CheckConstraint(
            "btrim(evidence_statement) <> ''",
            name="ck_evidence_cards_evidence_statement_not_blank",
        ),
        sa.CheckConstraint(
            "btrim(quote_text) <> ''",
            name="ck_evidence_cards_quote_text_not_blank",
        ),
        sa.CheckConstraint(
            "btrim(provider_key) <> ''",
            name="ck_evidence_cards_provider_key_not_blank",
        ),
        sa.CheckConstraint(
            "btrim(extractor_name) <> ''",
            name="ck_evidence_cards_extractor_name_not_blank",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(locator_refs) = 'array'",
            name="ck_evidence_cards_locator_refs_array",
        ),
        sa.UniqueConstraint(
            "evidence_fingerprint",
            name="uq_evidence_cards_evidence_fingerprint",
        ),
    )
    op.create_index("ix_evidence_cards_company_id", "evidence_cards", ["company_id"])
    op.create_index("ix_evidence_cards_source_id", "evidence_cards", ["source_id"])
    op.create_index("ix_evidence_cards_chunk_id", "evidence_cards", ["chunk_id"])
    op.create_index(
        "ix_evidence_cards_research_question_sha256",
        "evidence_cards",
        ["research_question_sha256"],
    )
    op.create_index("ix_evidence_cards_evidence_type", "evidence_cards", ["evidence_type"])
    op.create_index("ix_evidence_cards_created_at", "evidence_cards", ["created_at"])


def downgrade() -> None:
    # 数据安全：存在任何 EvidenceCard 时拒绝回滚，不静默丢弃证据。
    if _table_has_row("evidence_cards", "1=1"):
        raise RuntimeError(
            "cannot downgrade migration 0016: evidence_cards contains rows; "
            "refusing to drop evidence cards silently"
        )
    op.drop_table("evidence_cards")
