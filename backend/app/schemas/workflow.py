"""Pydantic contract for workflow run records."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.tasks import WorkflowRunStatus


class WorkflowRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_id: UUID
    task_id: UUID
    thread_id: str
    graph_name: str
    graph_version: str
    status: WorkflowRunStatus
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failed_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = Field(default=None, max_length=500)
    created_at: datetime
    updated_at: datetime
