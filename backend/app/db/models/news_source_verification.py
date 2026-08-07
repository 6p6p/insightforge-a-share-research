"""SQLAlchemy model for news original source verifications (stage 2D.2A).

一条 Candidate → SourceRecord 的验证溯源记录：记录"哪个原创发布者、从
requested_url 获取到 final_url、HTTP 状态、媒体类型、重定向次数、标题来源、
验证时间"。同一 candidate 唯一一条（uq_news_source_verifications_candidate）。

verified 语义（ADR-0015 不变量 D）严格限定为：原始发布网页属于登记的原创
媒体、公开 HTML 被安全获取并不可变归档、Candidate → SourceRecord 溯源已
建立；不表示新闻内容为真、不是 Evidence。
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
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class NewsSourceVerificationModel(Base):
    __tablename__ = "news_source_verifications"
    __table_args__ = (
        CheckConstraint(
            "btrim(requested_url) <> ''",
            name="ck_news_source_verifications_requested_url_not_blank",
        ),
        CheckConstraint(
            "btrim(final_url) <> ''",
            name="ck_news_source_verifications_final_url_not_blank",
        ),
        CheckConstraint(
            "btrim(final_hostname) <> ''",
            name="ck_news_source_verifications_final_hostname_not_blank",
        ),
        CheckConstraint(
            "http_status BETWEEN 200 AND 299",
            name="ck_news_source_verifications_http_status",
        ),
        CheckConstraint(
            "redirect_count BETWEEN 0 AND 5",
            name="ck_news_source_verifications_redirect_count",
        ),
        CheckConstraint(
            "title_origin IN ('discovery_candidate')",
            name="ck_news_source_verifications_title_origin",
        ),
        UniqueConstraint("candidate_id", name="uq_news_source_verifications_candidate"),
        Index("ix_news_source_verifications_source_id", "source_id"),
        Index(
            "ix_news_source_verifications_publisher_provider_key",
            "publisher_provider_key",
        ),
        Index("ix_news_source_verifications_verified_at", text("verified_at DESC")),
    )

    verification_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    candidate_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("news_discovery_candidates.candidate_id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("source_records.source_id", ondelete="RESTRICT"),
        nullable=False,
    )
    publisher_provider_key: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("source_providers.provider_key", ondelete="RESTRICT"),
        nullable=False,
    )
    requested_url: Mapped[str] = mapped_column(Text, nullable=False)
    final_url: Mapped[str] = mapped_column(Text, nullable=False)
    final_hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    http_status: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    redirect_count: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    title_origin: Mapped[str] = mapped_column(String(32), nullable=False)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
