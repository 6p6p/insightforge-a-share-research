"""SQLAlchemy model for evidence-bound section revisions (stage 5E.2A).

`draft_section_revisions` 记录一次 **已裁决 rewrite 的正文修订**：source 草稿 +
确定性 trigger artifact → 新正文草稿。revision 不可变，无 update API。

- `source_draft_section_id` FK `draft_sections.draft_section_id` RESTRICT、
  `revised_draft_section_id` FK `draft_sections.draft_section_id` RESTRICT——
  上游草稿存在期间 revision 不静默消失；`revised_draft_section_id` **UNIQUE**
  （一个修订结果只挂一条 revision link，并发同 revision → 最终 1 revised draft +
  1 revision link）；
- `revision_round` INTEGER >= 1（Stage5 loop 内轮次）、`trigger_type`
  VARCHAR(24)（deterministic_check / audit_rewrite / human_rewrite）；
- trigger 三选一（exactly one 非空）：`check_result_id` FK
  `report_check_results.check_result_id`、`review_action_id` FK
  `report_review_actions.review_action_id`、`human_decision_id` FK
  `human_review_decisions.human_decision_id`，其余为 NULL；
- `revision_schema_version` / `revision_fingerprint` CHAR(64) **UNIQUE**：
  派生输入（source draft + 上游 trigger artifact + outline/claim/evidence
  指纹 + feedback + writer 身份）的 SHA-256，同 revision 输入 → replay 同一行。
"""

import uuid
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"

# 三个 trigger FK 中恰好一个非空（spec E）。
_EXACTLY_ONE_TRIGGER = (
    "((review_action_id IS NOT NULL)::int + "
    "(check_result_id IS NOT NULL)::int + "
    "(human_decision_id IS NOT NULL)::int) = 1"
)


class DraftSectionRevisionModel(Base):
    __tablename__ = "draft_section_revisions"
    __table_args__ = (
        CheckConstraint(
            "revision_round >= 1",
            name="ck_draft_section_revisions_revision_round",
        ),
        CheckConstraint(
            "trigger_type IN ('deterministic_check','audit_rewrite','human_rewrite')",
            name="ck_draft_section_revisions_trigger_type",
        ),
        CheckConstraint(
            "revision_schema_version >= 1",
            name="ck_draft_section_revisions_revision_schema_version",
        ),
        CheckConstraint(
            _EXACTLY_ONE_TRIGGER,
            name="ck_draft_section_revisions_exactly_one_trigger",
        ),
        CheckConstraint(
            "source_draft_section_id <> revised_draft_section_id",
            name="ck_draft_section_revisions_source_ne_revised",
        ),
        CheckConstraint(
            f"revision_fingerprint {_SHA256_CHECK}",
            name="ck_draft_section_revisions_revision_fingerprint",
        ),
        UniqueConstraint(
            "revised_draft_section_id",
            name="uq_draft_section_revisions_revised_draft_section_id",
        ),
        UniqueConstraint(
            "revision_fingerprint",
            name="uq_draft_section_revisions_revision_fingerprint",
        ),
        Index("ix_draft_section_revisions_source_draft_section_id", "source_draft_section_id"),
        Index("ix_draft_section_revisions_review_action_id", "review_action_id"),
        Index("ix_draft_section_revisions_check_result_id", "check_result_id"),
        Index("ix_draft_section_revisions_human_decision_id", "human_decision_id"),
    )

    revision_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_draft_section_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("draft_sections.draft_section_id", ondelete="RESTRICT"),
        nullable=False,
    )
    revised_draft_section_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("draft_sections.draft_section_id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision_round: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(24), nullable=False)
    review_action_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("report_review_actions.review_action_id", ondelete="RESTRICT"),
        nullable=True,
    )
    check_result_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("report_check_results.check_result_id", ondelete="RESTRICT"),
        nullable=True,
    )
    human_decision_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("human_review_decisions.human_decision_id", ondelete="RESTRICT"),
        nullable=True,
    )
    revision_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    revision_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
