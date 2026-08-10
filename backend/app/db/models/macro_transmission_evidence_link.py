"""SQLAlchemy model for macro transmission ↔ evidence links (stage 4C.1A).

`macro_transmission_evidence_links` 记录一条传导链绑定了哪些 EvidenceCard 以及各
自的**角色**：

- role ∈ macro_driver（宏观事实，origin_type=macro_observation）/
  company_exposure（公司暴露事实，origin_type=document_chunk）/
  observed_effect（已观察到的公司影响，origin_type=document_chunk，可选）。
- PK(transmission_id, evidence_card_id, role)；UNIQUE(transmission_id,
  evidence_card_id)——同一证据对同一传导链只能一种角色。
- transmission_id FK chains **CASCADE**（删链删 link）；evidence_card_id FK
  evidence_cards **RESTRICT**（证据存在期间 link 不静默消失）。
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

_ROLE_CHECK = "role IN ('macro_driver','company_exposure','observed_effect')"


class MacroTransmissionEvidenceLinkModel(Base):
    __tablename__ = "macro_transmission_evidence_links"
    __table_args__ = (
        CheckConstraint(_ROLE_CHECK, name="ck_macro_transmission_evidence_links_role"),
        UniqueConstraint(
            "transmission_id",
            "evidence_card_id",
            name="uq_macro_transmission_evidence_links_transmission_evidence",
        ),
        # 真实 DB 已存在（migration 0023）：按 evidence_card_id 反查传道链。
        # metadata 必须真实描述 current DB，供 alembic check 不产生
        # remove_index drift。
        Index(
            "ix_macro_transmission_evidence_links_evidence_card_id",
            "evidence_card_id",
        ),
    )

    transmission_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("macro_transmission_chains.transmission_id", ondelete="CASCADE"),
        primary_key=True,
    )
    evidence_card_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("evidence_cards.evidence_card_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    role: Mapped[str] = mapped_column(String(16), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
