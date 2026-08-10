"""Pydantic contract for workflow run records."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.tasks import WorkflowEventType, WorkflowRunStatus


class WorkflowRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_id: UUID
    task_id: UUID | None = None
    thread_id: str
    graph_name: str
    graph_version: str
    status: WorkflowRunStatus
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failed_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = Field(default=None, max_length=500)
    pending_action: str | None = None
    created_at: datetime
    updated_at: datetime


class WorkflowActionRequest(BaseModel):
    action_type: Literal["approve_plan", "cancel", "retry"]


class WorkflowActionResponse(BaseModel):
    run: WorkflowRunResponse
    replayed: bool = False


class WorkflowEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_id: int
    run_id: UUID
    event_type: WorkflowEventType
    node_name: str | None = Field(default=None, max_length=64)
    stage: str | None = Field(default=None, max_length=64)
    progress: int | None = Field(default=None, ge=0, le=100)
    message: str = Field(max_length=500)
    payload: dict[str, object] = Field(default_factory=dict)
    created_at: datetime
