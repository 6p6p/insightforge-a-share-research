"""SQLAlchemy model for claim synthesis run ↔ claim input links (stage 4D.1A).

`claim_synthesis_input_links` 记录一个 Claim Synthesis run 的**显式输入 Claim
集合**（synthesis_id ↔ claim_id 多对多）。**输入选择是显式的**：调用方 / 未来
LangGraph state 提供 claim_ids，本表只登记边界，不做语义筛选。

- PK (synthesis_id, claim_id)：同一 run 对同一 Claim 只能一条输入；
- synthesis_id FK claim_synthesis_runs **CASCADE**（删 run 删 links）；
- claim_id FK claims **RESTRICT**（Claim 存在期间 link 不静默消失，保证
  provenance 可重放）；
- INDEX claim_id（反查某 Claim 被哪些 synthesis run 引用）。
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ClaimSynthesisInputLinkModel(Base):
    __tablename__ = "claim_synthesis_input_links"
    __table_args__ = (Index("ix_claim_synthesis_input_links_claim_id", "claim_id"),)

    synthesis_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("claim_synthesis_runs.synthesis_id", ondelete="CASCADE"),
        primary_key=True,
    )
    claim_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("claims.claim_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
