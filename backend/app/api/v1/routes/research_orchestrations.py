"""Minimal research orchestration API（7A.2B.2 spec U/V，0 业务逻辑）。

默认自动研究入口 + 状态投影 + 人工/重试 action。route 只做协议层 dispatch 到
application service（`get_research_orchestration_service`），**不执行业务**：
- `POST /tasks/{task_id}/orchestrations`：create/start（`start_orchestration`）；
- `GET /tasks/{task_id}/orchestrations/current`：task 当前 orchestration 投影；
- `GET /research-orchestrations/{orchestration_id}`：按 id 状态投影；
- `POST /research-orchestrations/{orchestration_id}/actions`：action dispatch
  （approve / rewrite / research / cancel → `act_on_orchestration`；retry →
  `retry_orchestration`）。comment 只对 human decision 生效。

现有 advanced WorkPlan execute 保持兼容（spec U）；SSE 聚合 / Web 一键按钮留
7A.2B.3（spec V）。
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.api.dependencies import get_research_orchestration_service
from app.research_orchestration.service import ResearchOrchestrationService
from app.schemas.research_orchestration import (
    ResearchOrchestrationActionRequest,
    ResearchOrchestrationResponse,
)

tasks_router = APIRouter(tags=["research-orchestrations"], prefix="/tasks")
orchestrations_router = APIRouter(tags=["research-orchestrations"])


def _to_response(result) -> ResearchOrchestrationResponse:
    """从 frozen dataclass 投影（字段名一一对应）。"""
    return ResearchOrchestrationResponse.model_validate(result.__dict__)


@tasks_router.post(
    "/{task_id}/orchestrations",
    response_model=ResearchOrchestrationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_orchestration(
    task_id: UUID,
    service: Annotated[ResearchOrchestrationService, Depends(get_research_orchestration_service)],
) -> ResearchOrchestrationResponse:
    """task → create/replay orchestration → 未终态则顶层 run（spec U）。"""
    return _to_response(await service.start_orchestration(task_id))


@tasks_router.get(
    "/{task_id}/orchestrations/current",
    response_model=ResearchOrchestrationResponse,
)
async def get_current_orchestration(
    task_id: UUID,
    service: Annotated[ResearchOrchestrationService, Depends(get_research_orchestration_service)],
) -> ResearchOrchestrationResponse:
    """task 当前 orchestration 状态投影（active 优先，否则最近一条）。"""
    return _to_response(await service.get_current_orchestration(task_id))


@orchestrations_router.get(
    "/research-orchestrations/{orchestration_id}",
    response_model=ResearchOrchestrationResponse,
)
async def get_orchestration(
    orchestration_id: UUID,
    service: Annotated[ResearchOrchestrationService, Depends(get_research_orchestration_service)],
) -> ResearchOrchestrationResponse:
    """按 id 返回 orchestration 状态投影。"""
    return _to_response(await service.get_orchestration(orchestration_id))


@orchestrations_router.post(
    "/research-orchestrations/{orchestration_id}/actions",
    response_model=ResearchOrchestrationResponse,
)
async def act_on_orchestration(
    orchestration_id: UUID,
    payload: ResearchOrchestrationActionRequest,
    service: Annotated[ResearchOrchestrationService, Depends(get_research_orchestration_service)],
) -> ResearchOrchestrationResponse:
    """action dispatch：retry → `retry_orchestration`；human decision / cancel →
    `act_on_orchestration`（spec U/N/P）。"""
    if payload.action == "retry":
        result = await service.retry_orchestration(orchestration_id)
    else:
        result = await service.act_on_orchestration(
            orchestration_id, payload.action, payload.comment
        )
    return _to_response(result)
