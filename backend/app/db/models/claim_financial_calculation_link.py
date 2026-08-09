"""SQLAlchemy model for claim ↔ financial calculation links (stage 4B.2C.1).

`claim_financial_calculation_links` 承载 Claim 与 FinancialCalculation 之间的
关系（supports / contradicts / context），形成 **Claim → Calculation →
Observation → EvidenceCard → Source** 完整可重算证据链。**关系属于本表**，
不在 financial_calculations 上增加 supports_claim / contradicts_claim。

- PK (claim_id, calculation_id, relation)：同一 Claim 对同一 Calculation 每个
  relation 只能一条；
- **UNIQUE(claim_id, calculation_id)**（migration 0022）：同 claim + 同
  calculation 的跨 relation 重复由数据库层强制拒绝（一个 Calculation 对同一
  Claim 只能一种 relation），应用层 FinancialClaimDraft 构造时也拒绝；
- claim_id FK **CASCADE**（删 Claim 删 links）；
- calculation_id FK **RESTRICT**（计算存在期间 link 不静默消失）。

**不把 FinancialCalculation 伪装成 EvidenceCard**：Calculation = derived
deterministic fact，EvidenceCard = source-backed fact，两者分层。本表只链接
Claim 与 Calculation；Claim ↔ source Evidence 的链接仍走 claim_evidence_links。
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


class ClaimFinancialCalculationLinkModel(Base):
    __tablename__ = "claim_financial_calculation_links"
    __table_args__ = (
        CheckConstraint(
            _RELATION_CHECK,
            name="ck_claim_financial_calculation_links_relation",
        ),
        UniqueConstraint(
            "claim_id",
            "calculation_id",
            name="uq_claim_financial_calculation_links_claim_calculation",
        ),
        Index("ix_claim_financial_calculation_links_calculation_id", "calculation_id"),
    )

    claim_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("claims.claim_id", ondelete="CASCADE"),
        primary_key=True,
    )
    calculation_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("financial_calculations.calculation_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    relation: Mapped[str] = mapped_column(String(16), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
