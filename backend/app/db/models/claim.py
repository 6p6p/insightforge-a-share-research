"""SQLAlchemy model for claims (stage 4A).

`claims` 是 **Source → Evidence → Claim → Report → Audit 证据链中 Claim 的
最小原子单元**：分析结论的确定性登记，不是 Evidence（后者是来源事实）。

- caller 只能提供语义输入（research_question / statement / analysis_domain /
  claim_kind / confidence / importance / analyst 身份 / 三组 evidence relation
  ids）；company / research_question_sha256 / claim_schema_version /
  claim_fingerprint / created_at 由 ClaimService 从真实 Evidence 派生。
- claim_fingerprint UNIQUE：同一完全相同 Claim → replay 同一行，并发最终
  只 1 个；statement / evidence relations / confidence / analyst version
  任一变化 → 新指纹 → 新行，旧行保留（修改观点 = 创建新 Claim，无 update API）。
- analysis_domain / claim_kind / confidence / importance 枚举白名单。
"""

import uuid
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
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
_ANALYSIS_DOMAIN_CHECK = (
    "analysis_domain IN ('financial','business','event','macro','risk','valuation')"
)
_CLAIM_KIND_CHECK = "claim_kind IN ('fact','inference','risk','relative_valuation')"
_CONFIDENCE_CHECK = "confidence IN ('low','medium','high')"
_IMPORTANCE_CHECK = "importance IN ('normal','critical')"


class ClaimModel(Base):
    __tablename__ = "claims"
    __table_args__ = (
        CheckConstraint(_ANALYSIS_DOMAIN_CHECK, name="ck_claims_analysis_domain"),
        CheckConstraint(_CLAIM_KIND_CHECK, name="ck_claims_claim_kind"),
        CheckConstraint(_CONFIDENCE_CHECK, name="ck_claims_confidence"),
        CheckConstraint(_IMPORTANCE_CHECK, name="ck_claims_importance"),
        CheckConstraint("claim_schema_version >= 1", name="ck_claims_claim_schema_version"),
        CheckConstraint("analyst_version >= 1", name="ck_claims_analyst_version"),
        CheckConstraint(
            f"research_question_sha256 {_SHA256_CHECK}",
            name="ck_claims_research_question_sha256",
        ),
        CheckConstraint(f"claim_fingerprint {_SHA256_CHECK}", name="ck_claims_claim_fingerprint"),
        CheckConstraint(
            "btrim(research_question) <> ''",
            name="ck_claims_research_question_not_blank",
        ),
        CheckConstraint("btrim(statement) <> ''", name="ck_claims_statement_not_blank"),
        CheckConstraint("btrim(analyst_name) <> ''", name="ck_claims_analyst_name_not_blank"),
        UniqueConstraint("claim_fingerprint", name="uq_claims_claim_fingerprint"),
        Index("ix_claims_company_id", "company_id"),
        Index("ix_claims_created_at", "created_at"),
        Index("ix_claims_research_question_sha256", "research_question_sha256"),
    )

    claim_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("companies.company_id", ondelete="RESTRICT"),
        nullable=False,
    )
    research_question: Mapped[str] = mapped_column(Text, nullable=False)
    research_question_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    analysis_domain: Mapped[str] = mapped_column(String(32), nullable=False)
    claim_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[str] = mapped_column(String(16), nullable=False)
    importance: Mapped[str] = mapped_column(String(16), nullable=False)
    analyst_name: Mapped[str] = mapped_column(String(64), nullable=False)
    analyst_version: Mapped[int] = mapped_column(Integer, nullable=False)
    analyst_model_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    claim_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    claim_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
