"""SQLAlchemy model for macro snapshot artifact links (stage 2C.2A).

把一次 Macro 获取中的原始 JSON 响应（RawArtifact）关联到 MacroDatasetSnapshot。
role 表示该响应扮演的角色；observations_page 必须带 page，元数据角色 page 为空。
content_type 去除参数后的基础类型必须为 application/json —— 由 2C.2B 的
PersistenceService 保证，不用触发器（见 ADR-0012）。
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
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_ROLE_CHECK = "role IN ('indicator_metadata','country_metadata','observations_page')"
_PAGE_RULE_CHECK = (
    "(role IN ('indicator_metadata','country_metadata') AND page IS NULL) OR "
    "(role = 'observations_page' AND page IS NOT NULL AND page >= 1)"
)
_RESPONSE_STATUS_CHECK = "response_status BETWEEN 200 AND 299"
_FINAL_HOSTNAME_CHECK = "btrim(final_hostname) <> ''"


class MacroSnapshotArtifactModel(Base):
    __tablename__ = "macro_snapshot_artifacts"
    __table_args__ = (
        CheckConstraint(_ROLE_CHECK, name="ck_macro_snapshot_artifacts_role"),
        CheckConstraint(_PAGE_RULE_CHECK, name="ck_macro_snapshot_artifacts_role_page"),
        CheckConstraint(_RESPONSE_STATUS_CHECK, name="ck_macro_snapshot_artifacts_response_status"),
        CheckConstraint(_FINAL_HOSTNAME_CHECK, name="ck_macro_snapshot_artifacts_final_hostname"),
        # NULLS NOT DISTINCT：元数据角色 page 恒为 NULL，也要求同 snapshot 同 role
        # 只能存在一条，否则 PostgreSQL 默认把 NULL 视为互不相同导致约束失效。
        UniqueConstraint(
            "snapshot_id",
            "role",
            "page",
            name="uq_macro_snapshot_artifacts_snapshot_role_page",
            postgresql_nulls_not_distinct=True,
        ),
        UniqueConstraint(
            "snapshot_id",
            "artifact_id",
            "role",
            "page",
            name="uq_macro_snapshot_artifacts_snapshot_artifact_role_page",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_macro_snapshot_artifacts_snapshot_id", "snapshot_id"),
        Index("ix_macro_snapshot_artifacts_artifact_id", "artifact_id"),
        Index("ix_macro_snapshot_artifacts_role", "role"),
    )

    snapshot_artifact_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    snapshot_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("macro_dataset_snapshots.snapshot_id", ondelete="CASCADE"),
        nullable=False,
    )
    artifact_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("raw_artifacts.artifact_id", ondelete="RESTRICT"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_status: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    final_hostname: Mapped[str] = mapped_column(String(253), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
