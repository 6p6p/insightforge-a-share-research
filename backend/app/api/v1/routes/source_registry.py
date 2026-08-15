"""Source provider registry endpoints (read-only + URL 自动解析)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_source_registry_service
from app.domain.companies import ExchangeCode
from app.domain.sources import AcquisitionMethod, SourceAuthorityTier, SourceCapability
from app.schemas.source_provider import (
    ResolveProviderRequest,
    ResolveProviderResponse,
    SourceProviderListResponse,
    SourceProviderResponse,
)
from app.services.source_registry_service import SourceRegistryService

router = APIRouter(tags=["source-registry"])


@router.get("/source-providers", response_model=SourceProviderListResponse)
async def list_providers(
    service: Annotated[SourceRegistryService, Depends(get_source_registry_service)],
    authority_tier: Annotated[SourceAuthorityTier | None, Query()] = None,
    capability: Annotated[SourceCapability | None, Query()] = None,
    acquisition_method: Annotated[AcquisitionMethod | None, Query()] = None,
    exchange: Annotated[ExchangeCode | None, Query()] = None,
    enabled_only: Annotated[bool, Query()] = True,
) -> SourceProviderListResponse:
    providers = await service.list_providers(
        authority_tier=authority_tier,
        capability=capability,
        acquisition_method=acquisition_method,
        exchange=exchange,
        enabled_only=enabled_only,
    )
    items = [SourceProviderResponse.model_validate(p) for p in providers]
    return SourceProviderListResponse(items=items, total=len(items))


@router.get("/source-providers/{provider_key}", response_model=SourceProviderResponse)
async def get_provider(
    provider_key: str,
    service: Annotated[SourceRegistryService, Depends(get_source_registry_service)],
) -> SourceProviderResponse:
    provider = await service.get_provider(provider_key)
    return SourceProviderResponse.model_validate(provider)


@router.post(
    "/source-providers/resolve",
    response_model=ResolveProviderResponse,
    summary="URL 自动解析来源平台",
)
async def resolve_provider(
    payload: ResolveProviderRequest,
    service: Annotated[SourceRegistryService, Depends(get_source_registry_service)],
) -> ResolveProviderResponse:
    """URL → provider 自动解析（V1.1 closure）。

    优先匹配该公司登记的官网域名（issuer_official），再匹配 provider
    allowlist；都不匹配 → 422 SourceUrlNotAllowed（前端提示手动选择）。
    """
    resolved = await service.resolve_provider_for_url(payload.company_id, payload.url)
    return ResolveProviderResponse(
        provider_key=resolved.provider_key,
        display_name=resolved.display_name,
        authority_tier=resolved.authority_tier,
        critical_claim_eligible=resolved.critical_claim_eligible,
        matched_by=resolved.matched_by,
    )
