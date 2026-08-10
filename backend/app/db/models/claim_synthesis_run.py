"""SQLAlchemy model for claim synthesis runs (stage 4D.1A).

`claim_synthesis_runs` 登记一次 **Claim Synthesis 综合输入**：一个 research
question + 一个 analysis cutoff 下，把调用方显式选出的 2..50 条 Claim（跨
analysis_domain：financial / macro / valuation / business / event / risk）
绑定为一个不可变集合。**本表不是 Report、不是 DraftSection**——它只记录
"综合阶段的输入集合与 provenance 边界"，供未来 LangGraph 合成节点消费。

- 全部语义字段由调用方提供（company_id / research_question / analysis_as_of /
  claim_ids）；research_question_sha256 / synthesis_schema_version /
  synthesis_fingerprint / created_at 由 SynthesisService 确定性派生。
- synthesis_fingerprint UNIQUE：同一完全相同 input（question / cutoff /
  claim set）→ replay 同一 run；任一变化 → 新指纹 → 新 run，旧行保留
  （**无 update API**）。input 提交顺序不影响指纹（claims 按 claim_id
  canonical 排序）。
- **不复制** Evidence / Calculation / Transmission / Comparison 的 ID 到本表：
  Claim → 各 domain 子表 → Evidence → Source 的 provenance 已在既有 schema 中
  （本表只引用 claims.claim_id，证明输入集边界）。
"""

import uuid
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"


class ClaimSynthesisRunModel(Base):
    __tablename__ = "claim_synthesis_runs"
    __table_args__ = (
        CheckConstraint(
            f"research_question_sha256 {_SHA256_CHECK}",
            name="ck_claim_synthesis_runs_research_question_sha256",
        ),
        CheckConstraint(
            f"synthesis_fingerprint {_SHA256_CHECK}",
            name="ck_claim_synthesis_runs_synthesis_fingerprint",
        ),
        CheckConstraint(
            "synthesis_schema_version >= 1",
            name="ck_claim_synthesis_runs_schema_version",
        ),
        CheckConstraint(
            "btrim(research_question) <> ''",
            name="ck_claim_synthesis_runs_research_question_not_blank",
        ),
        UniqueConstraint(
            "synthesis_fingerprint",
            name="uq_claim_synthesis_runs_synthesis_fingerprint",
        ),
        Index("ix_claim_synthesis_runs_company_id", "company_id"),
        Index("ix_claim_synthesis_runs_analysis_as_of", "analysis_as_of"),
    )

    synthesis_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("companies.company_id", ondelete="RESTRICT"),
        nullable=False,
    )
    research_question: Mapped[str] = mapped_column(Text, nullable=False)
    research_question_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    analysis_as_of: Mapped[date] = mapped_column(Date, nullable=False)
    synthesis_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    synthesis_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
