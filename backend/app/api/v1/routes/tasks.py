"""Research task API endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response, status

from app.api.dependencies import get_task_service
from app.core.errors import InvalidIdempotencyKey
from app.domain.tasks import TaskStatus
from app.schemas.task import TaskCreateRequest, TaskListResponse, TaskResponse
from app.services.task_service import TaskService

router = APIRouter(tags=["tasks"], prefix="/tasks")

_MAX_IDEMPOTENCY_KEY_LENGTH = 128


def _normalize_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    if not value or len(value) > _MAX_IDEMPOTENCY_KEY_LENGTH:
        raise InvalidIdempotencyKey()
    for char in value:
        if not 32 <= ord(char) <= 126:
            raise InvalidIdempotencyKey()
    return value


@router.post("", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: TaskCreateRequest,
    response: Response,
    service: Annotated[TaskService, Depends(get_task_service)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> TaskResponse:
    key = _normalize_idempotency_key(idempotency_key)
    result = await service.create_task(payload, key)
    if result.replayed:
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotent-Replayed"] = "true"
    else:
        response.headers["Idempotent-Replayed"] = "false"
    return result.task


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: UUID,
    service: Annotated[TaskService, Depends(get_task_service)],
) -> TaskResponse:
    return await service.get_task(task_id)


@router.get("", response_model=TaskListResponse)
async def list_tasks(
    service: Annotated[TaskService, Depends(get_task_service)],
    status: Annotated[TaskStatus | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TaskListResponse:
    return await service.list_tasks(status=status, limit=limit, offset=offset)
