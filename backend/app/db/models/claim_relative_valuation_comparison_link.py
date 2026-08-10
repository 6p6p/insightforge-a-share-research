"""SQLAlchemy model for claim ↔ relative valuation comparison links (stage 4C.2B.1).

`claim_relative_valuation_comparison_links` 承载 Claim 与 RelativeValuationComparison
之间的关系（supports / contradicts / context），形成 **Claim →
ClaimRelativeValuationComparisonLink → RelativeValuationComparison →
ValuationMetricObservation → EvidenceCard → Source** 完整可重算证据链，使
Audit 能重算 peer median / premium 并知道 judgment 基于哪些 peer comparisons。
**关系属于本表**，不在 relative_valuation_comparisons 上增加 supports_claim /
contradicts_claim。

- PK (claim_id, comparison_id, relation)：同一 Claim 对同一 Comparison 每个
  relation 只能一条；
- **UNIQUE(claim_id, comparison_id)**：同 claim + 同 comparison 的跨 relation
  重复由数据库层强制拒绝（一个 Comparison 对同一 Claim 只能一种 relation），
  应用层 ValuationClaimDraft 构造时也拒绝；
- claim_id FK **CASCADE**（删 Claim 删 links）；
- comparison_id FK **RESTRICT**（比较存在期间 link 不静默消失）。

**不把 RelativeValuationComparison 伪装成 EvidenceCard**：Comparison = derived
deterministic fact，EvidenceCard = source-backed fact，两者分层。本表只链接
Claim 与 Comparison；Claim ↔ source Evidence 的链接仍走 claim_evidence_links。
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_RELATION_CHECK = "relation IN ('supports','contradicts','context')"


class ClaimRelativeValuationComparisonLinkModel(Base):
    __tablename__ = "claim_relative_valuation_comparison_links"
    __table_args__ = (
        CheckConstraint(
            _RELATION_CHECK,
            name="ck_claim_relative_valuation_comparison_links_relation",
        ),
        UniqueConstraint(
            "claim_id",
            "comparison_id",
            name="uq_claim_relative_valuation_comparison_links_claim_comparison",
        ),
        Index(
            "ix_claim_relative_valuation_comparison_links_comparison_id",
            "comparison_id",
        ),
    )

    claim_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("claims.claim_id", ondelete="CASCADE"),
        primary_key=True,
    )
    comparison_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("relative_valuation_comparisons.comparison_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    relation: Mapped[str] = mapped_column(String(16), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
