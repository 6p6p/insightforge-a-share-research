"""Pydantic contracts for the minimal research orchestration API (7A.2B.2 spec U).

- `ResearchOrchestrationResponse`：orchestration 只读状态投影（orchestration_id /
  task_id / status / phase + 完整可审计元数据），从
  `ResearchOrchestrationResult`（frozen dataclass）投影；
- `ResearchOrchestrationActionRequest`：`POST /research-orchestrations/{id}/actions`
  请求体——action 枚举（approve / rewrite / research / cancel / retry）+ 可选 comment。
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

ORCHESTRATION_ACTION = Literal["approve", "rewrite", "research", "cancel", "retry"]


class ResearchOrchestrationResponse(BaseModel):
    """orchestration 状态投影（不携带 plan / child 正文）。"""

    model_config = ConfigDict(frozen=True)

    orchestration_id: UUID
    task_id: UUID
    research_plan_id: UUID | None
    status: str
    current_phase: str
    attempt_no: int
    retry_of_orchestration_id: UUID | None
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    replayed: bool = False


class ResearchOrchestrationActionRequest(BaseModel):
    """人工 / 重试 action 请求（spec U：cancel / retry / human approve / rewrite / research）。"""

    model_config = ConfigDict(frozen=True)

    action: ORCHESTRATION_ACTION
    comment: str | None = None
