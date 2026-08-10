"""SQLAlchemy model for claim synthesis results (stage 4D.1B).

`claim_synthesis_results` 保存一次 **不可变的结构化综合结果**：对一个
SynthesisRun（research question + analysis cutoff + 已验证 Claim 输入集，
4D.1A 已登记）做 LLM 综合后，把 themes / claim roles / duplicates / conflicts /
evidence gaps / summary 结构化落库。**本表不是 Report、不是 DraftSection、不是
Audit**。

- synthesis_id FK `claim_synthesis_runs` RESTRICT——run 存在期间结果不静默消失；
  结果不可变，无 update API；
- result_schema_version / result_fingerprint / analyst_name / analyst_version /
  analyst_model_id 由 SynthesisAnalysisService 确定性派生；themes / claim_roles /
  duplicates / conflicts / evidence_gaps 为 JSONB（应用层 strict validation
  保证结构 + no cherry-picking：claim_roles 恰好覆盖每条输入 Claim）；
- result_fingerprint UNIQUE：同 run + 同 analyst 版本 + 同输出 → replay 同一行；
  任一变化 → 新指纹 → 新结果（旧行保留）；
- **不复制** Evidence / Calculation / Transmission / Comparison 的 ID；不存
  prompt / raw provider response / reasoning_content。
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
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"


class ClaimSynthesisResultModel(Base):
    __tablename__ = "claim_synthesis_results"
    __table_args__ = (
        CheckConstraint(
            "result_schema_version >= 1",
            name="ck_claim_synthesis_results_schema_version",
        ),
        CheckConstraint(
            f"result_fingerprint {_SHA256_CHECK}",
            name="ck_claim_synthesis_results_fingerprint",
        ),
        CheckConstraint(
            "analyst_version >= 1",
            name="ck_claim_synthesis_results_analyst_version",
        ),
        CheckConstraint(
            "btrim(summary) <> ''",
            name="ck_claim_synthesis_results_summary_not_blank",
        ),
        UniqueConstraint(
            "result_fingerprint",
            name="uq_claim_synthesis_results_fingerprint",
        ),
        Index("ix_claim_synthesis_results_synthesis_id", "synthesis_id"),
        Index("ix_claim_synthesis_results_created_at", "created_at"),
    )

    synthesis_result_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    synthesis_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("claim_synthesis_runs.synthesis_id", ondelete="RESTRICT"),
        nullable=False,
    )
    result_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    result_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    themes: Mapped[list] = mapped_column(JSONB, nullable=False)
    claim_roles: Mapped[list] = mapped_column(JSONB, nullable=False)
    duplicates: Mapped[list] = mapped_column(JSONB, nullable=False)
    conflicts: Mapped[list] = mapped_column(JSONB, nullable=False)
    evidence_gaps: Mapped[list] = mapped_column(JSONB, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    analyst_name: Mapped[str] = mapped_column(Text, nullable=False)
    analyst_version: Mapped[int] = mapped_column(Integer, nullable=False)
    analyst_model_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
