"""SQLAlchemy model for macro transmission chains (stage 4C.1A).

`macro_transmission_chains` 持久化**宏观传导分析产物**：Macro Evidence +
Company Exposure Evidence → 一条传导链 → Macro Claim。传导链描述"宏观变量如何
通过某个渠道传到公司"（如 利率 → financing channel → 公司有息负债 → 融资成本
压力），**不是 EvidenceCard**——它是分析产物，禁止伪装成来源事实。

- claim_id UNIQUE：一条传导链 = 一个 Macro Claim 专属，删 Claim 级联删链。
- channel_type ∈ revenue/cost/financing/demand/supply_chain/trade_policy/
  operations/other；effect_direction ∈ tailwind/headwind/mixed/uncertain（不是
  buy/sell）；impact_status ∈ plausible_impact/observed_impact；time_alignment
  ∈ aligned/uncertain（**无 misaligned**：证据明确错位时 Service 拒绝）。
- transmission_fingerprint UNIQUE：同一完全相同传导 → replay 同一行，并发最终
  只 1 条；语义任一变化 → 新指纹 → 新链，旧链保留（无 update API）。
- 非空 / 枚举 / 版本 / fingerprint 全由 DB CHECK 兜底，Service 层先拒绝。
"""

import uuid
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"
_CHANNEL_TYPE_CHECK = (
    "channel_type IN ('revenue','cost','financing','demand','supply_chain',"
    "'trade_policy','operations','other')"
)
_EFFECT_DIRECTION_CHECK = "effect_direction IN ('tailwind','headwind','mixed','uncertain')"
_IMPACT_STATUS_CHECK = "impact_status IN ('plausible_impact','observed_impact')"
_TIME_ALIGNMENT_CHECK = "time_alignment IN ('aligned','uncertain')"


class MacroTransmissionChainModel(Base):
    __tablename__ = "macro_transmission_chains"
    __table_args__ = (
        CheckConstraint(
            f"transmission_fingerprint {_SHA256_CHECK}",
            name="ck_macro_transmission_chains_fingerprint",
        ),
        CheckConstraint(
            "transmission_schema_version >= 1",
            name="ck_macro_transmission_chains_schema_version",
        ),
        CheckConstraint(_CHANNEL_TYPE_CHECK, name="ck_macro_transmission_chains_channel_type"),
        CheckConstraint(
            _EFFECT_DIRECTION_CHECK,
            name="ck_macro_transmission_chains_effect_direction",
        ),
        CheckConstraint(
            _IMPACT_STATUS_CHECK,
            name="ck_macro_transmission_chains_impact_status",
        ),
        CheckConstraint(
            _TIME_ALIGNMENT_CHECK,
            name="ck_macro_transmission_chains_time_alignment",
        ),
        UniqueConstraint("claim_id", name="uq_macro_transmission_chains_claim_id"),
        UniqueConstraint(
            "transmission_fingerprint",
            name="uq_macro_transmission_chains_transmission_fingerprint",
        ),
    )

    transmission_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    claim_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("claims.claim_id", ondelete="CASCADE"),
        nullable=False,
    )
    company_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("companies.company_id", ondelete="RESTRICT"),
        nullable=False,
    )
    channel_type: Mapped[str] = mapped_column(String(32), nullable=False)
    effect_direction: Mapped[str] = mapped_column(String(16), nullable=False)
    impact_status: Mapped[str] = mapped_column(String(16), nullable=False)
    time_alignment: Mapped[str] = mapped_column(String(16), nullable=False)
    transmission_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    transmission_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
