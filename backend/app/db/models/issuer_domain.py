"""SQLAlchemy model for issuer official website domains (V1.1 final closure).

`issuer_domains`：上市公司官网域名 registry——`issuer_official` 受控来源的
域名校验依据（不降低现有 allowlist / SSRF 策略）：

- company_id 绑定（官网属于特定公司，不允许任意网站伪装 issuer）；
- domain：真实主机名（如 `www.catl.com`），URL hostname 匹配校验；
- source_url：官网验证 URL（https，记录 provenance）；
- provider_key：固定 `issuer_official`（source_providers 中登记）；
- UNIQUE(company_id, domain)：同一公司同一域名只登记一次。
"""

import uuid
from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class IssuerDomainModel(Base):
    __tablename__ = "issuer_domains"
    __table_args__ = (
        CheckConstraint(
            "btrim(domain) <> ''",
            name="ck_issuer_domains_domain_not_blank",
        ),
        CheckConstraint(
            "source_url ~ '^https://'",
            name="ck_issuer_domains_url_https",
        ),
        UniqueConstraint(
            "company_id",
            "domain",
            name="uq_issuer_domains_company_domain",
        ),
        Index("ix_issuer_domains_domain", "domain"),
    )

    domain_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("companies.company_id", ondelete="CASCADE"),
        nullable=False,
    )
    domain: Mapped[str] = mapped_column(String(200), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    provider_key: Mapped[str] = mapped_column(String(32), nullable=False)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
