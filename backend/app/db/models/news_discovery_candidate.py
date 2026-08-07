"""SQLAlchemy model for news discovery candidates (stage 2D.1).

一条发现候选：rank、标题、原始 URL、确定性 normalization 后的 URL、
url_sha256、派生 domain、seen_at 与 Provider 附带的 language/country，
以及 verification_status（本阶段固定 unverified，2D.2 才演进）。候选只是
线索，不访问 URL、不下载正文、不是 SourceRecord / Evidence。同一 run 内
rank 与 normalized_url 均唯一。engine 不引用 SourceProvider。
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
from sqlalchemy.dialects.postgresql import CHAR
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"
_RANK_CHECK = "rank >= 1"
_TITLE_CHECK = "btrim(title) <> ''"
_NORMALIZED_URL_CHECK = "btrim(normalized_url) <> ''"
_DOMAIN_CHECK = "btrim(domain) <> ''"
_VERIFICATION_STATUS_CHECK = "verification_status = 'unverified'"


class NewsDiscoveryCandidateModel(Base):
    __tablename__ = "news_discovery_candidates"
    __table_args__ = (
        CheckConstraint(_RANK_CHECK, name="ck_news_discovery_candidates_rank"),
        CheckConstraint(_TITLE_CHECK, name="ck_news_discovery_candidates_title_not_blank"),
        CheckConstraint(
            _NORMALIZED_URL_CHECK,
            name="ck_news_discovery_candidates_normalized_url_not_blank",
        ),
        CheckConstraint(
            f"url_sha256 {_SHA256_CHECK}",
            name="ck_news_discovery_candidates_url_sha256",
        ),
        CheckConstraint(_DOMAIN_CHECK, name="ck_news_discovery_candidates_domain_not_blank"),
        CheckConstraint(
            _VERIFICATION_STATUS_CHECK,
            name="ck_news_discovery_candidates_verification_status",
        ),
        UniqueConstraint(
            "discovery_run_id",
            "rank",
            name="uq_news_discovery_candidates_run_rank",
        ),
        UniqueConstraint(
            "discovery_run_id",
            "normalized_url",
            name="uq_news_discovery_candidates_run_normalized_url",
        ),
        Index("ix_news_discovery_candidates_run_id", "discovery_run_id"),
        Index("ix_news_discovery_candidates_domain", "domain"),
        Index("ix_news_discovery_candidates_seen_at", text("seen_at DESC")),
        Index("ix_news_discovery_candidates_url_sha256", "url_sha256"),
    )

    candidate_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    discovery_run_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("news_discovery_runs.discovery_run_id", ondelete="CASCADE"),
        nullable=False,
    )
    rank: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    title: Mapped[str] = mapped_column(String(1000), nullable=False)
    discovered_url: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_url: Mapped[str] = mapped_column(Text, nullable=False)
    url_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    domain: Mapped[str] = mapped_column(String(253), nullable=False)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_language: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_country: Mapped[str | None] = mapped_column(String(100), nullable=True)
    verification_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'unverified'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
