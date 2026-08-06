"""Source provider registry endpoints (read-only)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_source_registry_service
from app.domain.companies import ExchangeCode
from app.domain.sources import AcquisitionMethod, SourceAuthorityTier, SourceCapability
from app.schemas.source_provider import (
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
