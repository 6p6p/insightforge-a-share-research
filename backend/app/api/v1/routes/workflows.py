"""Workflow run and event endpoints."""

import asyncio
import time
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse

from app.api.dependencies import (
    get_workflow_execution_manager,
    get_workflow_service,
)
from app.core.errors import InvalidLastEventId
from app.schemas.workflow import WorkflowRunResponse
from app.services.sse_service import format_sse_event
from app.services.workflow_service import WorkflowService
from app.workflows.execution_manager import WorkflowExecutionManager

router = APIRouter(tags=["workflows"])

_POLL_INTERVAL_SECONDS = 1.0
_KEEPALIVE_INTERVAL_SECONDS = 15.0
_MAX_EVENTS_PER_POLL = 100


def _parse_last_event_id(value: str | None) -> int:
    if value is None:
        return 0
    try:
        parsed = int(value)
    except ValueError:
        raise InvalidLastEventId() from None
    if parsed < 0:
        raise InvalidLastEventId()
    return parsed


@router.post(
    "/tasks/{task_id}/runs",
    response_model=WorkflowRunResponse,
    status_code=202,
)
async def start_run(
    task_id: UUID,
    manager: Annotated[WorkflowExecutionManager, Depends(get_workflow_execution_manager)],
) -> WorkflowRunResponse:
    """Create and schedule a deterministic simulation workflow run (not real research)."""
    return await manager.start_simulation(task_id)


@router.get("/workflow-runs/{run_id}", response_model=WorkflowRunResponse)
async def get_run(
    run_id: UUID,
    service: Annotated[WorkflowService, Depends(get_workflow_service)],
) -> WorkflowRunResponse:
    return await service.get_run(run_id)


@router.get("/workflow-runs/{run_id}/events")
async def stream_events(
    run_id: UUID,
    request: Request,
    service: Annotated[WorkflowService, Depends(get_workflow_service)],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    # StreamingResponse 创建前确认 run 存在并校验 Last-Event-ID
    await service.get_run(run_id)
    cursor = _parse_last_event_id(last_event_id)

    async def event_generator():
        current = cursor
        last_keepalive = time.monotonic()
        yield ": connected\n\n"
        while True:
            if await request.is_disconnected():
                break
            events = await service.list_events_after(run_id, current, _MAX_EVENTS_PER_POLL)
            for event in events:
                yield format_sse_event(event)
                current = event.event_id
            if events:
                last_keepalive = time.monotonic()
                continue
            if await service.is_terminal(run_id):
                # terminal 后再查一轮，确保 run_completed 等剩余事件已发出
                events = await service.list_events_after(run_id, current, _MAX_EVENTS_PER_POLL)
                for event in events:
                    yield format_sse_event(event)
                    current = event.event_id
                break
            if time.monotonic() - last_keepalive >= _KEEPALIVE_INTERVAL_SECONDS:
                yield ": keep-alive\n\n"
                last_keepalive = time.monotonic()
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
