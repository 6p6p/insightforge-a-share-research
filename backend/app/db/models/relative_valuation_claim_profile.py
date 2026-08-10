"""SQLAlchemy model for relative valuation claim profiles (stage 4C.2B.1).

`relative_valuation_claim_profiles` 记录 Relative Valuation Claim 的**分析
判断层**语义（1:1 with `claims.claim_id`）：assessment（relative_high /
broadly_in_line / relative_low / mixed / uncertain）与 analysis_as_of。

- claim_id UUID PK FK claims **CASCADE**（删 Claim 删 profile）；
- assessment = **Analyst 的结构化判断**（结构化语义输入），不是程序从
  premium 自动推导的公式输出；程序不写 hidden thresholds；**不做**
  buy/sell/bullish/bearish/cheap/expensive（买卖建议边界）；
- analysis_as_of = 判断对应的研究时点（与 selected comparisons 的
  comparison.analysis_as_of 完全一致，ClaimService 校验）；
- profile_schema_version >= 1 CHECK：升级 = 新 fingerprint = 新 Claim，历史
  profile 原样保留（无 update API）。
"""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_ASSESSMENT_CHECK = (
    "assessment IN ('relative_high','broadly_in_line','relative_low','mixed','uncertain')"
)


class RelativeValuationClaimProfileModel(Base):
    __tablename__ = "relative_valuation_claim_profiles"
    __table_args__ = (
        CheckConstraint(
            _ASSESSMENT_CHECK,
            name="ck_relative_valuation_claim_profiles_assessment",
        ),
        CheckConstraint(
            "profile_schema_version >= 1",
            name="ck_relative_valuation_claim_profiles_schema_version",
        ),
    )

    claim_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("claims.claim_id", ondelete="CASCADE"),
        primary_key=True,
    )
    assessment: Mapped[str] = mapped_column(String(32), nullable=False)
    analysis_as_of: Mapped[date] = mapped_column(Date, nullable=False)
    profile_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
