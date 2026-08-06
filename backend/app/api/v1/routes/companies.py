"""Company identity endpoints (read-only)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from app.api.dependencies import get_company_identity_service
from app.schemas.company import (
    CompanyIdentityResponse,
    CompanyResolutionResponse,
    CompanyResolveRequest,
)
from app.services.company_identity_service import CompanyIdentityService

router = APIRouter(tags=["companies"])


@router.post("/companies/resolve", response_model=CompanyResolutionResponse)
async def resolve_company(
    payload: CompanyResolveRequest,
    service: Annotated[CompanyIdentityService, Depends(get_company_identity_service)],
) -> CompanyResolutionResponse:
    """Resolve a company query to an identity (exact match only)."""
    return await service.resolve(payload.query)


@router.get("/companies/{company_id}", response_model=CompanyIdentityResponse)
async def get_company(
    company_id: UUID,
    service: Annotated[CompanyIdentityService, Depends(get_company_identity_service)],
) -> CompanyIdentityResponse:
    return await service.get_company(company_id)
