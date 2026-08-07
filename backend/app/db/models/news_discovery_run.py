"""SQLAlchemy model for news discovery runs (stage 2D.1).

一次"搜索-发现"过程的持久化记录：保存搜索表达式、时间窗、Engine、状态、
HTTP 请求次数，以及原始响应归档引用与冗余的响应元数据（便于审计，不随
RawArtifact 查询）。query_fingerprint 唯一标识"同 engine + 公司 + 搜索
表达式 + 时间范围 + max_results + 原始响应 sha256"的一次发现，重复响应
replay 到同一 Run。Run 不是 SourceRecord、不是 Evidence。
"""

import uuid
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"
_ENGINE_CHECK = "engine = 'gdelt_doc'"
_QUERY_TEXT_CHECK = "btrim(query_text) <> ''"
_WINDOW_ORDER_CHECK = "query_start_at <= query_end_at"
_MAX_RESULTS_CHECK = "max_results BETWEEN 1 AND 100"
_RESULT_COUNT_CHECK = "result_count >= 0"
_REQUEST_COUNT_CHECK = "request_count >= 1"
_RESPONSE_STATUS_CHECK = "response_status BETWEEN 200 AND 299"
_FINAL_HOSTNAME_CHECK = "btrim(final_hostname) <> ''"
_STATUS_CHECK = "status = 'available'"


class NewsDiscoveryRunModel(Base):
    __tablename__ = "news_discovery_runs"
    __table_args__ = (
        CheckConstraint(_ENGINE_CHECK, name="ck_news_discovery_runs_engine"),
        CheckConstraint(
            _QUERY_TEXT_CHECK,
            name="ck_news_discovery_runs_query_text_not_blank",
        ),
        CheckConstraint(_WINDOW_ORDER_CHECK, name="ck_news_discovery_runs_window_order"),
        CheckConstraint(_MAX_RESULTS_CHECK, name="ck_news_discovery_runs_max_results"),
        CheckConstraint(
            f"raw_content_sha256 {_SHA256_CHECK}",
            name="ck_news_discovery_runs_raw_content_sha256",
        ),
        CheckConstraint(
            f"query_fingerprint {_SHA256_CHECK}",
            name="ck_news_discovery_runs_query_fingerprint",
        ),
        CheckConstraint(_RESULT_COUNT_CHECK, name="ck_news_discovery_runs_result_count"),
        CheckConstraint(_REQUEST_COUNT_CHECK, name="ck_news_discovery_runs_request_count"),
        CheckConstraint(_RESPONSE_STATUS_CHECK, name="ck_news_discovery_runs_response_status"),
        CheckConstraint(
            _FINAL_HOSTNAME_CHECK,
            name="ck_news_discovery_runs_final_hostname_not_blank",
        ),
        CheckConstraint(_STATUS_CHECK, name="ck_news_discovery_runs_status"),
        UniqueConstraint("query_fingerprint", name="uq_news_discovery_runs_query_fingerprint"),
        Index("ix_news_discovery_runs_company_id", "company_id"),
        Index("ix_news_discovery_runs_fetched_at", text("fetched_at DESC")),
        Index("ix_news_discovery_runs_raw_artifact_id", "raw_artifact_id"),
    )

    discovery_run_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("companies.company_id", ondelete="RESTRICT"),
        nullable=False,
    )
    engine: Mapped[str] = mapped_column(String(32), nullable=False)
    query_text: Mapped[str] = mapped_column(String(300), nullable=False)
    query_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    query_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    max_results: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    raw_artifact_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("raw_artifacts.artifact_id", ondelete="RESTRICT"),
        nullable=False,
    )
    raw_content_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    result_count: Mapped[int] = mapped_column(nullable=False)
    request_count: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    response_status: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    final_hostname: Mapped[str] = mapped_column(String(253), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    query_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'available'")
    )
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
