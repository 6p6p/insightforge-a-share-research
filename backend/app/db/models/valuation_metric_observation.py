"""SQLAlchemy model for valuation metric observations (stage 4C.2A).

`valuation_metric_observations` 把来源于真实文档 Evidence 的**原始估值倍数数值**
登记为确定性的数值事实（P/E、P/B、P/S），供后续（4C.2A comparison /
4C.2B Relative Valuation Analyst）做确定性相对估值。

- provenance 通过 `source_evidence_card_id`（FK evidence_cards RESTRICT）回到
  EvidenceCard → quote → DocumentChunk → ParsedSource → SourceRecord →
  RawArtifact → locator；**不复制 locator_refs** 到本表。
- `metric_as_of` = **市场观测日**（该倍数对应的估值时点，如 2026-08-07），
  **不是来源发布时间**；不要求 source availability <= metric_as_of（数据源可能
  次日才发布更晚的估值）。
- `metric_value` 完全由 `source_value_text` 按 Financial 同一 numeric grammar
  解析得到（全 Decimal，无 float）；允许 0 / 负值（来源事实快照）。
- `valuation_observation_fingerprint` UNIQUE：同一完全相同 observation →
  replay 同一行，并发最终只 1 行；value / metric / date / source evidence 任一
  变化 → 新指纹 → 新行，旧行保留（修订 = 新 observation，无 update API）。
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
_METRIC_CODE_CHECK = "metric_code IN ('pe_ttm','pb_mrq','ps_ttm')"


class ValuationMetricObservationModel(Base):
    __tablename__ = "valuation_metric_observations"
    __table_args__ = (
        CheckConstraint(
            _METRIC_CODE_CHECK,
            name="ck_valuation_metric_observations_metric_code",
        ),
        CheckConstraint(
            f"valuation_observation_fingerprint {_SHA256_CHECK}",
            name="ck_valuation_metric_observations_fingerprint",
        ),
        CheckConstraint(
            "valuation_observation_schema_version >= 1",
            name="ck_valuation_metric_observations_schema_version",
        ),
        CheckConstraint(
            "btrim(source_value_text) <> ''",
            name="ck_valuation_metric_observations_source_value_text_not_blank",
        ),
        UniqueConstraint(
            "valuation_observation_fingerprint",
            name="uq_valuation_metric_observations_fingerprint",
        ),
        Index("ix_valuation_metric_observations_company_id", "company_id"),
        Index(
            "ix_valuation_metric_observations_source_evidence_card_id",
            "source_evidence_card_id",
        ),
        Index("ix_valuation_metric_observations_metric_code", "metric_code"),
        Index("ix_valuation_metric_observations_metric_as_of", "metric_as_of"),
    )

    valuation_observation_id: Mapped[UUID] = mapped_column(
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
    metric_code: Mapped[str] = mapped_column(String(16), nullable=False)
    metric_as_of: Mapped[date] = mapped_column(Date, nullable=False)
    source_value_text: Mapped[str] = mapped_column(Text, nullable=False)
    metric_value: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    valuation_observation_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    valuation_observation_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
