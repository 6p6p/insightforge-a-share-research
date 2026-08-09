"""SQLAlchemy model for evidence cards (stage 3C.1).

`evidence_cards` 是 **Evidence 链的原子证据单元**：已确认与研究问题相关、
有明确原文片段和 provenance 的确定性登记。

- 证据链 provenance 全量快照：company / source / parsed_source / chunk_set /
  chunk 五个 FK 全部 RESTRICT（上游存在期间 EvidenceCard 不会被级联删除）。
  provider_key / source_published_at / reporting_period_end /
  authority_tier_snapshot / critical_claim_eligible_snapshot 由 Service 从
  真实 provenance 派生，调用方不可提供。
- quote 精确切片自 chunk.text[quote_start:quote_end]（程序生成），
  quote_sha256 64 位小写 hex，quote_text trim 后非空。
- locator_refs 是 **quote 级**投影（project_evidence_locator_refs 输出）。
- evidence_fingerprint UNIQUE：同一完全相同 Evidence → replay 同一卡，
  并发最终只 1 张；语义 / quote / extractor version 任一变化 → 新指纹 →
  新卡，旧卡保留（修订 = 新 EvidenceCard，无 update API）。
- extractor_confidence（语义提取置信度）≠ authority_tier_snapshot（来源
  可靠性）；critical_claim_eligible_snapshot 直接复制 SourceRecord，不会因
  extractor_confidence=high 自动提升。
"""

import uuid
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"
_EVIDENCE_TYPE_CHECK = "evidence_type IN ('fact','metric','event','statement','context')"
_CONFIDENCE_CHECK = "extractor_confidence IN ('low','medium','high')"
_ORIGIN_TYPE_CHECK = "origin_type IN ('document_chunk','macro_observation')"
_ORIGIN_CONSISTENCY_CHECK = """
(
  (origin_type = 'document_chunk' AND
     source_id IS NOT NULL AND parsed_source_id IS NOT NULL AND
     chunk_set_id IS NOT NULL AND chunk_id IS NOT NULL AND
     quote_start IS NOT NULL AND quote_end IS NOT NULL AND
     quote_text IS NOT NULL AND quote_sha256 IS NOT NULL AND
     macro_observation_id IS NULL AND macro_snapshot_id IS NULL AND macro_series_id IS NULL)
  OR
  (origin_type = 'macro_observation' AND
     macro_observation_id IS NOT NULL AND macro_snapshot_id IS NOT NULL AND
     macro_series_id IS NOT NULL AND
     source_id IS NULL AND parsed_source_id IS NULL AND
     chunk_set_id IS NULL AND chunk_id IS NULL AND
     quote_start IS NULL AND quote_end IS NULL AND
     quote_text IS NULL AND quote_sha256 IS NULL)
)
"""


class EvidenceCardModel(Base):
    __tablename__ = "evidence_cards"
    __table_args__ = (
        CheckConstraint(
            _ORIGIN_TYPE_CHECK,
            name="ck_evidence_cards_origin_type",
        ),
        CheckConstraint(
            _ORIGIN_CONSISTENCY_CHECK,
            name="ck_evidence_cards_origin_consistency",
        ),
        CheckConstraint(
            "quote_start >= 0",
            name="ck_evidence_cards_quote_start",
        ),
        CheckConstraint(
            "quote_end > quote_start",
            name="ck_evidence_cards_quote_end",
        ),
        CheckConstraint(
            _EVIDENCE_TYPE_CHECK,
            name="ck_evidence_cards_evidence_type",
        ),
        CheckConstraint(
            _CONFIDENCE_CHECK,
            name="ck_evidence_cards_extractor_confidence",
        ),
        CheckConstraint(
            "extractor_version >= 1",
            name="ck_evidence_cards_extractor_version",
        ),
        CheckConstraint(
            "evidence_schema_version >= 1",
            name="ck_evidence_cards_evidence_schema_version",
        ),
        CheckConstraint(
            f"research_question_sha256 {_SHA256_CHECK}",
            name="ck_evidence_cards_research_question_sha256",
        ),
        CheckConstraint(
            f"quote_sha256 {_SHA256_CHECK}",
            name="ck_evidence_cards_quote_sha256",
        ),
        CheckConstraint(
            f"evidence_fingerprint {_SHA256_CHECK}",
            name="ck_evidence_cards_evidence_fingerprint",
        ),
        CheckConstraint(
            "authority_tier_snapshot BETWEEN 1 AND 4",
            name="ck_evidence_cards_authority_tier_snapshot",
        ),
        CheckConstraint(
            "btrim(research_question) <> ''",
            name="ck_evidence_cards_research_question_not_blank",
        ),
        CheckConstraint(
            "btrim(evidence_statement) <> ''",
            name="ck_evidence_cards_evidence_statement_not_blank",
        ),
        CheckConstraint(
            "btrim(quote_text) <> ''",
            name="ck_evidence_cards_quote_text_not_blank",
        ),
        CheckConstraint(
            "btrim(provider_key) <> ''",
            name="ck_evidence_cards_provider_key_not_blank",
        ),
        CheckConstraint(
            "btrim(extractor_name) <> ''",
            name="ck_evidence_cards_extractor_name_not_blank",
        ),
        CheckConstraint(
            "jsonb_typeof(locator_refs) = 'array'",
            name="ck_evidence_cards_locator_refs_array",
        ),
        CheckConstraint(
            "jsonb_array_length(locator_refs) > 0",
            name="ck_evidence_cards_locator_refs_nonempty",
        ),
        UniqueConstraint(
            "evidence_fingerprint",
            name="uq_evidence_cards_evidence_fingerprint",
        ),
        Index("ix_evidence_cards_company_id", "company_id"),
        Index("ix_evidence_cards_source_id", "source_id"),
        Index("ix_evidence_cards_chunk_id", "chunk_id"),
        Index("ix_evidence_cards_research_question_sha256", "research_question_sha256"),
        Index("ix_evidence_cards_evidence_type", "evidence_type"),
        Index("ix_evidence_cards_created_at", "created_at"),
        Index("ix_evidence_cards_origin_type", "origin_type"),
        Index("ix_evidence_cards_macro_observation_id", "macro_observation_id"),
    )

    evidence_card_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    origin_type: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="document_chunk"
    )
    company_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("companies.company_id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("source_records.source_id", ondelete="RESTRICT"),
        nullable=True,
    )
    parsed_source_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("parsed_sources.parsed_source_id", ondelete="RESTRICT"),
        nullable=True,
    )
    chunk_set_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("chunk_sets.chunk_set_id", ondelete="RESTRICT"),
        nullable=True,
    )
    chunk_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("document_chunks.chunk_id", ondelete="RESTRICT"),
        nullable=True,
    )
    macro_observation_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("macro_observations.observation_id", ondelete="RESTRICT"),
        nullable=True,
    )
    macro_snapshot_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("macro_dataset_snapshots.snapshot_id", ondelete="RESTRICT"),
        nullable=True,
    )
    macro_series_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("macro_series.series_id", ondelete="RESTRICT"),
        nullable=True,
    )
    research_question: Mapped[str] = mapped_column(Text, nullable=False)
    research_question_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    evidence_statement: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(16), nullable=False)
    quote_start: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    quote_end: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    quote_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    quote_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    locator_refs: Mapped[list] = mapped_column(JSONB, nullable=False)
    provider_key: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("source_providers.provider_key", ondelete="RESTRICT"),
        nullable=False,
    )
    source_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reporting_period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    authority_tier_snapshot: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    critical_claim_eligible_snapshot: Mapped[bool] = mapped_column(Boolean, nullable=False)
    extractor_name: Mapped[str] = mapped_column(String(64), nullable=False)
    extractor_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    extractor_model_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    extractor_confidence: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
