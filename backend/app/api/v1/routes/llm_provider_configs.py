"""LLM provider config API endpoints (v1.2.7-B)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from app.api.dependencies import get_llm_provider_config_service
from app.schemas.llm_provider_config import (
    LlmConfigCreateRequest,
    LlmConfigListResponse,
    LlmConfigResponse,
    LlmConfigSetActiveRequest,
    LlmConfigTestRequest,
    LlmConfigTestResponse,
    LlmConfigUpdateRequest,
)
from app.services.llm_provider_config_service import LlmProviderConfigService

router = APIRouter(tags=["llm-provider-configs"], prefix="/llm-configs")


@router.get("", response_model=LlmConfigListResponse)
async def list_llm_configs(
    service: Annotated[LlmProviderConfigService, Depends(get_llm_provider_config_service)],
) -> LlmConfigListResponse:
    items = await service.list()
    active_id = next((item.id for item in items if item.is_active), None)
    return LlmConfigListResponse(items=items, total=len(items), active_id=active_id)


@router.post("", response_model=LlmConfigResponse, status_code=status.HTTP_201_CREATED)
async def create_llm_config(
    payload: LlmConfigCreateRequest,
    service: Annotated[LlmProviderConfigService, Depends(get_llm_provider_config_service)],
) -> LlmConfigResponse:
    return await service.create(payload)


@router.get("/{config_id}", response_model=LlmConfigResponse)
async def get_llm_config(
    config_id: UUID,
    service: Annotated[LlmProviderConfigService, Depends(get_llm_provider_config_service)],
) -> LlmConfigResponse:
    return await service.get(config_id)


@router.put("/{config_id}", response_model=LlmConfigResponse)
async def update_llm_config(
    config_id: UUID,
    payload: LlmConfigUpdateRequest,
    service: Annotated[LlmProviderConfigService, Depends(get_llm_provider_config_service)],
) -> LlmConfigResponse:
    return await service.update(config_id, payload)


@router.delete("/{config_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_llm_config(
    config_id: UUID,
    service: Annotated[LlmProviderConfigService, Depends(get_llm_provider_config_service)],
) -> Response:
    await service.delete(config_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{config_id}/active", response_model=LlmConfigResponse)
async def set_active_config(
    config_id: UUID,
    payload: LlmConfigSetActiveRequest,
    service: Annotated[LlmProviderConfigService, Depends(get_llm_provider_config_service)],
) -> LlmConfigResponse:
    return await service.set_active(config_id, payload.is_active)


@router.post("/{config_id}/test", response_model=LlmConfigTestResponse)
async def test_stored_config(
    config_id: UUID,
    service: Annotated[LlmProviderConfigService, Depends(get_llm_provider_config_service)],
) -> LlmConfigTestResponse:
    config = await service.get(config_id)
    request = LlmConfigTestRequest(
        provider=config.provider,
        model_id=config.model_id,
        base_url=config.base_url,
        api_key=None,
        use_stored_key=True,
    )
    return await service.test_connection(request)


@router.post("/test", response_model=LlmConfigTestResponse)
async def test_llm_config(
    payload: LlmConfigTestRequest,
    service: Annotated[LlmProviderConfigService, Depends(get_llm_provider_config_service)],
) -> LlmConfigTestResponse:
    return await service.test_connection(payload)
