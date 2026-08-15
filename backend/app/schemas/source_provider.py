"""Pydantic contracts for source providers."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class SourceProviderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    provider_key: str
    display_name: str
    provider_type: str
    authority_tier: int
    homepage_url: str
    allowed_domains: list[str]
    capabilities: list[str]
    acquisition_methods: list[str]
    exchange_scope: list[str]
    requires_api_key: bool
    critical_claim_eligible: bool
    enabled: bool


class SourceProviderListResponse(BaseModel):
    items: list[SourceProviderResponse]
    total: int


class ResolveProviderRequest(BaseModel):
    """URL → provider 自动解析请求（V1.1 closure）。"""

    company_id: UUID
    url: str


class ResolveProviderResponse(BaseModel):
    """URL → provider 自动解析结果（matched_by=issuer_domain | allowed_domain）。"""

    provider_key: str
    display_name: str
    authority_tier: int
    critical_claim_eligible: bool
    matched_by: str
