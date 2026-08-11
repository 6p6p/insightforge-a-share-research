"""SQLAlchemy model for deterministic report export (stage 6C).

`report_exports` 保存一次 **不可变、确定性导出**：把一次已验证 `VerifiedReport`
+ `VerifiedReportCheckResult` + `VerifiedReportAudit`（+ 可选人工批准
`VerifiedHumanReviewDecision`）机械地渲染成 Markdown / DOCX / PDF 字节并内容寻址
归档（0 LLM / 0 Retrieval / 0 Chroma / 0 Web，renderer 不查 DB）。

- `task_id` FK `research_tasks.task_id` RESTRICT、`report_id` FK
  `reports.report_id` RESTRICT——上游存在期间 Export 不静默消失；Export 不可变，
  无 update API（同输入 → replay 同一行）；
- `check_result_id` / `audit_id` FK（RESTRICT）把导出绑定到**它自己引用的**
  check / audit，`verify_export_integrity` 按这些行独立重验，不依赖 task 的
  canonical lineage 是否变化；`human_decision_id` NULL（audit pass 路径）或
  FK `human_review_decisions`（人工批准路径，RESTRICT）；
- `export_schema_version` / `export_format`（markdown/docx/pdf，CHECK）/
  `export_input_fingerprint` CHAR(64) **UNIQUE**（= export schema + task +
  report / check / audit / decision 指纹 + format + renderer 身份 + normalized
  pack 身份 的 SHA-256，**并发唯一性来源**，同输入 → replay 同一行）；
- `content_sha256` / `byte_size` / `media_type` / `file_name` / `storage_key`
  描述归档字节（内容寻址路径，见 `ExportArtifactStore`）。
"""

import uuid
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
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
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"


class ReportExportModel(Base):
    __tablename__ = "report_exports"
    __table_args__ = (
        CheckConstraint(
            "export_schema_version >= 1",
            name="ck_report_exports_export_schema_version",
        ),
        CheckConstraint(
            "export_format IN ('markdown','docx','pdf')",
            name="ck_report_exports_export_format",
        ),
        CheckConstraint(
            f"export_input_fingerprint {_SHA256_CHECK}",
            name="ck_report_exports_export_input_fingerprint",
        ),
        CheckConstraint(
            f"content_sha256 {_SHA256_CHECK}",
            name="ck_report_exports_content_sha256",
        ),
        CheckConstraint(
            "byte_size > 0",
            name="ck_report_exports_byte_size",
        ),
        UniqueConstraint(
            "export_input_fingerprint",
            name="uq_report_exports_export_input_fingerprint",
        ),
        Index("ix_report_exports_task_id", "task_id"),
        Index("ix_report_exports_report_id", "report_id"),
    )

    export_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("research_tasks.task_id", ondelete="RESTRICT"),
        nullable=False,
    )
    report_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("reports.report_id", ondelete="RESTRICT"),
        nullable=False,
    )
    check_result_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("report_check_results.check_result_id", ondelete="RESTRICT"),
        nullable=False,
    )
    audit_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("report_audits.audit_id", ondelete="RESTRICT"),
        nullable=False,
    )
    human_decision_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("human_review_decisions.human_decision_id", ondelete="RESTRICT"),
        nullable=True,
    )
    export_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    export_format: Mapped[str] = mapped_column(String(16), nullable=False)
    export_input_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    content_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
