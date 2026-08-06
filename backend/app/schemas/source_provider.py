"""Pydantic contracts for source providers."""

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
