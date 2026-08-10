"""SQLAlchemy model for relative valuation comparisons (stage 4C.2A).

`relative_valuation_comparisons` 记录一次**显式 peer 相对估值比较**的确定性
派生结果：目标公司同一 metric / 同一 metric_as_of 下，对显式 peer 集合计算
peer_median / peer_min / peer_max / premium_discount_to_median。

- **Comparison 不是 EvidenceCard**：相对估值比较是程序确定性派生的比较事实
  （4C.2B Analyst 的输入），不是来源事实；来源事实由 target / peer
  Observation → EvidenceCard → Source 承载（provenance 经 observation 链接，
  **不复制全部 evidence id** 到本表）。
- `target_company_id` / `target_observation_id` FK RESTRICT；peer 明细在
  `relative_valuation_comparison_peers`。
- `analysis_as_of >= metric_as_of` CHECK（no-lookahead：分析时点不能早于市场
  观测日）。
- v1 只有 comparison_method='peer_median'（确定性公式，无 LLM / 无自动选 peer /
  无分类——relative_high/reasonable/relative_low 属 4C.2B Analyst 判断）。
- `comparison_fingerprint` UNIQUE：同一完全相同 comparison → replay 同一行，
  并发最终只 1 行 + 1 套完整 peer links；任一输入变化 → 新指纹 → 新 comparison，
  旧行保留（无 update API）。
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
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"
_METRIC_CODE_CHECK = "metric_code IN ('pe_ttm','pb_mrq','ps_ttm')"
_METHOD_CHECK = "comparison_method IN ('peer_median')"


class RelativeValuationComparisonModel(Base):
    __tablename__ = "relative_valuation_comparisons"
    __table_args__ = (
        CheckConstraint(
            _METRIC_CODE_CHECK,
            name="ck_relative_valuation_comparisons_metric_code",
        ),
        CheckConstraint(_METHOD_CHECK, name="ck_relative_valuation_comparisons_method"),
        CheckConstraint(
            "peer_count BETWEEN 3 AND 20",
            name="ck_relative_valuation_comparisons_peer_count",
        ),
        CheckConstraint(
            "analysis_as_of >= metric_as_of",
            name="ck_relative_valuation_comparisons_no_lookahead",
        ),
        CheckConstraint(
            "formula_version >= 1",
            name="ck_relative_valuation_comparisons_formula_version",
        ),
        CheckConstraint(
            "comparison_schema_version >= 1",
            name="ck_relative_valuation_comparisons_schema_version",
        ),
        CheckConstraint(
            f"comparison_fingerprint {_SHA256_CHECK}",
            name="ck_relative_valuation_comparisons_fingerprint",
        ),
        UniqueConstraint(
            "comparison_fingerprint",
            name="uq_relative_valuation_comparisons_fingerprint",
        ),
        Index("ix_relative_valuation_comparisons_target_company_id", "target_company_id"),
    )

    comparison_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    target_company_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("companies.company_id", ondelete="RESTRICT"),
        nullable=False,
    )
    target_observation_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "valuation_metric_observations.valuation_observation_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    metric_code: Mapped[str] = mapped_column(String(16), nullable=False)
    metric_as_of: Mapped[date] = mapped_column(Date, nullable=False)
    analysis_as_of: Mapped[date] = mapped_column(Date, nullable=False)
    comparison_method: Mapped[str] = mapped_column(String(32), nullable=False)
    peer_count: Mapped[int] = mapped_column(Integer, nullable=False)
    peer_median: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    peer_min: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    peer_max: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    premium_discount_to_median: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    comparison_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    formula_version: Mapped[int] = mapped_column(Integer, nullable=False)
    comparison_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
