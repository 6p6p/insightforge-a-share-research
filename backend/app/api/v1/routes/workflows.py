"""Workflow run and event endpoints."""

import asyncio
import time
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import StreamingResponse

from app.api.dependencies import (
    get_research_execution_service,
    get_workflow_execution_manager,
    get_workflow_service,
)
from app.core.errors import WorkflowActionInvalid
from app.domain.tasks import HumanActionType
from app.schemas.workflow import (
    WorkflowActionRequest,
    WorkflowActionResponse,
    WorkflowRunResponse,
)
from app.services.research_execution_service import ResearchExecutionService
from app.services.sse_service import format_sse_event, parse_last_event_id
from app.services.workflow_service import WorkflowService
from app.stage5.contracts import STAGE5_GRAPH_NAME
from app.workflows.execution_manager import WorkflowExecutionManager

router = APIRouter(tags=["workflows"])

_POLL_INTERVAL_SECONDS = 1.0
_KEEPALIVE_INTERVAL_SECONDS = 15.0
_MAX_EVENTS_PER_POLL = 100


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


_STAGE5_HUMAN_DECISIONS = frozenset({"approve", "rewrite", "research", "cancel"})


@router.post(
    "/workflow-runs/{run_id}/actions",
    response_model=WorkflowActionResponse,
)
async def run_action(
    run_id: UUID,
    payload: WorkflowActionRequest,
    response: Response,
    manager: Annotated[WorkflowExecutionManager, Depends(get_workflow_execution_manager)],
    service: Annotated[WorkflowService, Depends(get_workflow_service)],
    research_execution: Annotated[
        ResearchExecutionService, Depends(get_research_execution_service)
    ],
) -> WorkflowActionResponse:
    """Submit a human action for a workflow run.

    - Stage 1 simulation run：approve_plan / cancel / retry（既有语义不变）；
    - Stage 5 真实研究 run：approve / rewrite / research / cancel（经
      ResearchExecutionService.resume_human，复用 Stage5WorkflowRunner）。
    """
    run = await service.get_run(run_id)
    if run.graph_name == STAGE5_GRAPH_NAME:
        if payload.action_type not in _STAGE5_HUMAN_DECISIONS:
            raise WorkflowActionInvalid()
        if payload.action_type == "cancel":
            resolved = await research_execution.cancel(run_id)
        else:
            resolved = await research_execution.resume_human(
                run_id, payload.action_type, payload.comment
            )
        response.status_code = 202
        return WorkflowActionResponse(run=resolved)
    if payload.action_type == "approve_plan":
        resolved = await manager.resume_simulation(run_id, HumanActionType.APPROVE_PLAN)
        response.status_code = 202
        return WorkflowActionResponse(run=resolved)
    if payload.action_type == "cancel":
        resolved = await manager.cancel_run(run_id)
        response.status_code = 202
        return WorkflowActionResponse(run=resolved)
    if payload.action_type == "retry":
        resolved = await manager.retry_run(run_id)
        response.status_code = 202
        return WorkflowActionResponse(run=resolved)
    raise WorkflowActionInvalid()


@router.get("/workflow-runs/{run_id}/events")
async def stream_events(
    run_id: UUID,
    request: Request,
    service: Annotated[WorkflowService, Depends(get_workflow_service)],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    # StreamingResponse 创建前确认 run 存在并校验 Last-Event-ID
    await service.get_run(run_id)
    cursor = parse_last_event_id(last_event_id)

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
