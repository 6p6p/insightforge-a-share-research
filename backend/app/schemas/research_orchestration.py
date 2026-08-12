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
    """orchestration 状态投影（7A Product Gate spec O；不携带 plan / child 正文）。

    checkpoint 派生字段（current_child_run_id / backflow_round / research_request_id /
    manual_reason / missing_need_codes）由 service `_project` 从顶层 checkpoint
    补充，**不放过** Evidence body / prompt / reasoning。
    """

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
    # 7A Product Gate spec O：checkpoint 派生（可空，未进入对应阶段 / 无 checkpoint 时 None）。
    current_child_run_id: UUID | None = None
    backflow_round: int | None = None
    research_request_id: UUID | None = None
    manual_reason: str | None = None
    missing_need_codes: list[str] | None = None
    updated_at: datetime | None = None


class ResearchOrchestrationActionRequest(BaseModel):
    """人工 / 重试 action 请求（spec U：cancel / retry / human approve / rewrite / research）。"""

    model_config = ConfigDict(frozen=True)

    action: ORCHESTRATION_ACTION
    comment: str | None = None
