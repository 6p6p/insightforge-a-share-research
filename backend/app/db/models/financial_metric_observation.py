"""SQLAlchemy model for financial metric observations (stage 4B.2A).

`financial_metric_observations` 把来源于真实财务 Evidence 的**原始财务数值**
登记为确定性的数值事实，供后续（4B.2B）确定性财务计算。

- provenance 通过 `source_evidence_card_id` 回到 EvidenceCard → quote →
  DocumentChunk → ParsedSource → SourceRecord → RawArtifact → locator；
  **不复制 locator_refs** 到本表，PG EvidenceCard 继续是 provenance truth
  source。
- company_id / source_evidence_card_id 双 FK RESTRICT：上游存在期间本行不会
  被级联删除。
- raw_value 完全由 `source_value_text` 解析得到；normalized_value_cny 由
  raw_value × raw_unit 系数（全部 Decimal，无 float）。
- metric_fingerprint UNIQUE：同一完全相同 observation → replay 同一行，
  并发最终只 1 行；value / unit / period / metric code / source evidence
  任一变化 → 新指纹 → 新行，旧行保留（修订 = 新 observation，无 update API）。
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"
_STATEMENT_SCOPE_CHECK = "statement_scope IN ('consolidated','parent')"
_PERIOD_KIND_CHECK = "period_kind IN ('duration','instant')"
_RAW_UNIT_CHECK = "raw_unit IN ('yuan','thousand_yuan','ten_thousand_yuan','hundred_million_yuan')"
# period 一致性：instant → period_start 必须 NULL；duration → period_start
# 必须非空且 <= period_end。
_PERIOD_CONSISTENCY_CHECK = """
(
  (period_kind = 'instant' AND period_start IS NULL)
  OR
  (period_kind = 'duration' AND period_start IS NOT NULL AND period_start <= period_end)
)
"""


class FinancialMetricObservationModel(Base):
    __tablename__ = "financial_metric_observations"
    __table_args__ = (
        CheckConstraint(
            _STATEMENT_SCOPE_CHECK,
            name="ck_financial_metric_observations_statement_scope",
        ),
        CheckConstraint(
            _PERIOD_KIND_CHECK,
            name="ck_financial_metric_observations_period_kind",
        ),
        CheckConstraint(
            _RAW_UNIT_CHECK,
            name="ck_financial_metric_observations_raw_unit",
        ),
        CheckConstraint(
            _PERIOD_CONSISTENCY_CHECK,
            name="ck_financial_metric_observations_period_consistency",
        ),
        CheckConstraint(
            f"metric_fingerprint {_SHA256_CHECK}",
            name="ck_financial_metric_observations_metric_fingerprint",
        ),
        CheckConstraint(
            "metric_schema_version >= 1",
            name="ck_financial_metric_observations_metric_schema_version",
        ),
        CheckConstraint(
            "btrim(metric_code) <> ''",
            name="ck_financial_metric_observations_metric_code_not_blank",
        ),
        CheckConstraint(
            "btrim(source_value_text) <> ''",
            name="ck_financial_metric_observations_source_value_text_not_blank",
        ),
        UniqueConstraint(
            "metric_fingerprint",
            name="uq_financial_metric_observations_metric_fingerprint",
        ),
        Index("ix_financial_metric_observations_company_id", "company_id"),
        Index(
            "ix_financial_metric_observations_source_evidence_card_id",
            "source_evidence_card_id",
        ),
        Index("ix_financial_metric_observations_metric_code", "metric_code"),
        Index("ix_financial_metric_observations_period_end", "period_end"),
    )

    metric_observation_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("companies.company_id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_evidence_card_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("evidence_cards.evidence_card_id", ondelete="RESTRICT"),
        nullable=False,
    )
    metric_code: Mapped[str] = mapped_column(String(64), nullable=False)
    statement_scope: Mapped[str] = mapped_column(String(16), nullable=False)
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    period_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_value_text: Mapped[str] = mapped_column(Text, nullable=False)
    raw_value: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    raw_unit: Mapped[str] = mapped_column(String(32), nullable=False)
    normalized_value_cny: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    metric_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    metric_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
