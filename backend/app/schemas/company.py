"""Pydantic contracts for company identity."""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.companies import CompanyMatchType


class CompanyIdentityResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    company_id: UUID
    exchange: str
    security_code: str
    identity_key: str
    board: str
    official_name: str
    short_name: str
    listing_status: str
    listing_date: date | None = None
    delisting_date: date | None = None
    identity_source_provider_key: str
    identity_source_url: str
    source_updated_at: datetime | None = None


class CompanyResolveRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    query: str = Field(min_length=1, max_length=200)


class CompanyResolutionResponse(BaseModel):
    company: CompanyIdentityResponse
    match_type: CompanyMatchType
    matched_value: str
