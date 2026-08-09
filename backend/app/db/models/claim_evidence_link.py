"""SQLAlchemy model for claim ↔ evidence links (stage 4A).

`claim_evidence_links` 承载 Claim 与 EvidenceCard 之间的关系（supports /
contradicts / context）。**关系属于本表**，不在 evidence_cards 上增加
supports_claim / contradicts_claim。

- PK (claim_id, evidence_card_id, relation)：同一 Claim 对同一 Evidence 每个
  relation 只能一条（v1 禁止同一卡跨 relation 重复，ClaimDraft 构造时拒绝）；
- UNIQUE(claim_id, evidence_card_id)（migration 0019）：同 claim + 同 evidence
  的跨 relation 重复由数据库层强制拒绝，不再只依赖应用层约束；
- claim_id FK CASCADE（删 Claim 删 links）；
- evidence_card_id FK RESTRICT（证据存在期间 link 不静默消失）。
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


class ClaimEvidenceLinkModel(Base):
    __tablename__ = "claim_evidence_links"
    __table_args__ = (
        CheckConstraint(_RELATION_CHECK, name="ck_claim_evidence_links_relation"),
        UniqueConstraint(
            "claim_id",
            "evidence_card_id",
            name="uq_claim_evidence_links_claim_evidence",
        ),
        Index("ix_claim_evidence_links_evidence_card_id", "evidence_card_id"),
        Index("ix_claim_evidence_links_relation", "relation"),
    )

    claim_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("claims.claim_id", ondelete="CASCADE"),
        primary_key=True,
    )
    evidence_card_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("evidence_cards.evidence_card_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    relation: Mapped[str] = mapped_column(String(16), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
