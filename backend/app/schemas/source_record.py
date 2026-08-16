"""Pydantic contracts for source records."""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.source_records import SourceDocumentType


class SourceRecordResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source_id: UUID
    company_id: UUID
    provider_key: str
    artifact_id: UUID
    document_type: SourceDocumentType
    title: str
    published_at: datetime | None = None
    reporting_period_end: date | None = None
    source_url: str | None = None
    acquisition_method: str
    external_document_id: str | None = None
    authority_tier_snapshot: int
    critical_claim_eligible_snapshot: bool
    provider_capabilities_snapshot: list[str]
    status: str
    acquired_at: datetime
    created_at: datetime
    content_sha256: str
    byte_size: int
    media_type: str


class SourceRecordListResponse(BaseModel):
    items: list[SourceRecordResponse]
    total: int
    limit: int
    offset: int


class SourceUrlImportRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    company_id: UUID
    provider_key: str = Field(min_length=1, max_length=32)
    document_type: SourceDocumentType
    title: str = Field(min_length=1, max_length=500)
    source_url: str = Field(min_length=1, max_length=2000)
    published_at: datetime | None = None
    reporting_period_end: date | None = None
    external_document_id: str | None = Field(default=None, max_length=200)
