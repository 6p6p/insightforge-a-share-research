"""Canonical public task status projection regression tests (Product Consistency).

真实 PostgreSQL：task + orchestration rows → TaskService.list/get / workspace 投影
**所有位置**必须一致显示五态（未开始/进行中/等待确认/已完成/失败）。

回归场景（用户验收）：
- completed orchestration + report → task list = 已完成、workspace = 已完成、
  progress = 已完成（同一 canonical 值，不再出现「研究完成/待执行/已创建」并存）；
- awaiting human review（waiting_human + awaiting_stage5）→ 所有位置 = 等待确认；
- 无 orchestration → 未开始；running → 进行中；failed → 失败。
"""

from datetime import date
from uuid import UUID

import pytest
import pytest_asyncio

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.research_orchestration import ResearchOrchestrationModel
from app.db.models.research_task import ResearchTaskModel
from app.db.session import DatabaseManager
from app.repositories.research_task_repository import ResearchTaskRepository
from app.services.task_service import TaskService
from app.services.task_status_projection import (
    PUBLIC_STATUS_COMPLETED,
    PUBLIC_STATUS_FAILED,
    PUBLIC_STATUS_IN_PROGRESS,
    PUBLIC_STATUS_NOT_STARTED,
    PUBLIC_STATUS_WAITING_CONFIRMATION,
    project_completed_with_warnings,
    project_public_status,
)
from app.services.task_workspace_service import TaskWorkspaceService

pytestmark = pytest.mark.integration

configure_asyncio_runtime()


@pytest_asyncio.fixture
async def database() -> DatabaseManager:
    settings = get_settings()
    manager = DatabaseManager(
        database_url=settings.database_url,
        echo=False,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    yield manager
    await manager.dispose()


@pytest_asyncio.fixture
async def sessionmaker(database):
    return database.session_factory()


def _task_model(*, status: str = "pending") -> ResearchTaskModel:
    return ResearchTaskModel(
        company_query="600519",
        research_start_date=date(2023, 1, 1),
        research_end_date=date(2025, 12, 31),
        modules=["company_profile"],
        questions=[],
        include_relative_valuation=False,
        require_plan_approval=False,
        status=status,
        current_stage="created",
        progress=0,
    )


def _orchestration_model(
    task_id: UUID,
    *,
    status: str,
    phase: str,
) -> ResearchOrchestrationModel:
    return ResearchOrchestrationModel(
        task_id=task_id,
        research_plan_id=None,
        attempt_no=1,
        retry_of_orchestration_id=None,
        orchestration_schema_version=1,
        orchestrator_name="research_orchestrator",
        orchestrator_version=1,
        status=status,
        current_phase=phase,
        input_fingerprint="0" * 64,
        started_at=None,
        completed_at=None,
        error_code=None,
        error_message=None,
    )


async def _seed(sessionmaker, *, task_status: str = "pending", orchestration=None):
    """建 task（+ 可选 orchestration），返回 task_id。"""
    async with sessionmaker() as session:
        task = _task_model(status=task_status)
        await ResearchTaskRepository(session).create(task)
        if orchestration is not None:
            session.add(
                _orchestration_model(task.task_id, status=orchestration[0], phase=orchestration[1])
            )
        await session.commit()
        return task.task_id


async def _list_public_status(sessionmaker, task_id: UUID) -> str:
    async with sessionmaker() as session:
        service = TaskService(ResearchTaskRepository(session), sessionmaker)
        listing = await service.list_tasks(status=None, limit=50, offset=0)
    return next(item.public_status for item in listing.items if item.task_id == task_id)


async def _workspace_public_status(sessionmaker, task_id: UUID) -> str:
    service = TaskWorkspaceService(sessionmaker)
    workspace = await service.get_workspace(task_id)
    return workspace.task.public_status


# ---------------------------------------------------------------- 纯函数


def test_project_public_status_table() -> None:
    """纯函数投影表：task/orchestration 组合 → canonical 五态。"""
    assert project_public_status(task_status="pending") == PUBLIC_STATUS_NOT_STARTED
    assert project_public_status(task_status="completed") == PUBLIC_STATUS_COMPLETED
    assert project_public_status(task_status="failed") == PUBLIC_STATUS_FAILED
    assert project_public_status(task_status="running") == PUBLIC_STATUS_IN_PROGRESS
    assert (
        project_public_status(task_status="pending", orchestration_status="completed")
        == PUBLIC_STATUS_COMPLETED
    )
    assert (
        project_public_status(task_status="pending", orchestration_status="running")
        == PUBLIC_STATUS_IN_PROGRESS
    )
    assert (
        project_public_status(task_status="pending", orchestration_status="waiting_human")
        == PUBLIC_STATUS_WAITING_CONFIRMATION
    )
    assert (
        project_public_status(task_status="pending", orchestration_status="failed")
        == PUBLIC_STATUS_FAILED
    )
    # task 显式失败终态优先于任何 orchestration 状态。
    assert (
        project_public_status(task_status="failed", orchestration_status="completed")
        == PUBLIC_STATUS_FAILED
    )


def test_project_public_status_completed_with_warnings_is_terminal_completed() -> None:
    """v1.2.6：completed_with_warnings（人工接受带审核提醒的报告）→ 已完成
    （terminal completed），绝不落入 in_progress fallback。"""
    assert (
        project_public_status(task_status="pending", orchestration_status="completed_with_warnings")
        == PUBLIC_STATUS_COMPLETED
    )
    # 任务列表/工作台/概要均显示已完成（区别于普通 completed 由 bool 信号展示）。
    assert (
        project_completed_with_warnings(
            task_status="pending", orchestration_status="completed_with_warnings"
        )
        is True
    )
    assert (
        project_completed_with_warnings(task_status="pending", orchestration_status="completed")
        is False
    )
    assert (
        project_completed_with_warnings(task_status="pending", orchestration_status=None) is False
    )


# ---------------------------------------------------------------- regression：已完成


@pytest.mark.asyncio
async def test_completed_orchestration_all_positions_completed(sessionmaker) -> None:
    """用户验收：completed orchestration + report → task list / workspace 全部
    显示「已完成」（此前 task.status 恒 pending → 列表显示「待执行/已创建」）。"""
    task_id = await _seed(sessionmaker, orchestration=("completed", "completed"))

    assert await _list_public_status(sessionmaker, task_id) == PUBLIC_STATUS_COMPLETED
    assert await _workspace_public_status(sessionmaker, task_id) == PUBLIC_STATUS_COMPLETED

    # get_task 同一 projection。
    async with sessionmaker() as session:
        service = TaskService(ResearchTaskRepository(session), sessionmaker)
        fetched = await service.get_task(task_id)
    assert fetched.public_status == PUBLIC_STATUS_COMPLETED
    # 底层 task.status 仍是 pending（未修改 LangGraph / DB 语义——投影是权威）。
    assert fetched.status.value == "pending"


# ---------------------------------------------------------------- v1.2.6：带提醒完成


@pytest.mark.asyncio
async def test_completed_with_warnings_all_positions_completed(sessionmaker) -> None:
    """用户验收：orchestration=completed_with_warnings（人工接受带审核提醒）→
    task list / workspace / get_task 全部显示「已完成」，且不出现在 running
    fallback；completed_with_warnings 信号同时透出（前端据此显示「已完成
    （包含审核提醒）」）。真实 DB 状态保留（task.status 仍是 pending，
    orchestration.status 仍是 completed_with_warnings——投影是权威）。"""
    task_id = await _seed(sessionmaker, orchestration=("completed_with_warnings", "completed"))

    assert await _list_public_status(sessionmaker, task_id) == PUBLIC_STATUS_COMPLETED
    assert await _workspace_public_status(sessionmaker, task_id) == PUBLIC_STATUS_COMPLETED

    async with sessionmaker() as session:
        service = TaskService(ResearchTaskRepository(session), sessionmaker)
        fetched = await service.get_task(task_id)
    assert fetched.public_status == PUBLIC_STATUS_COMPLETED
    assert fetched.completed_with_warnings is True
    # 真实状态未被修改：task 仍 pending，orchestration 仍 completed_with_warnings。
    assert fetched.status.value == "pending"
    from sqlalchemy import select

    async with sessionmaker() as session:
        from app.db.models.research_orchestration import ResearchOrchestrationModel

        row = (
            await session.execute(
                select(ResearchOrchestrationModel).where(
                    ResearchOrchestrationModel.task_id == task_id
                )
            )
        ).scalar_one()
        assert row.status == "completed_with_warnings"


@pytest.mark.asyncio
async def test_workspace_completed_with_warnings_signal(sessionmaker) -> None:
    """workspace 投影的 task 亦携带 completed_with_warnings 信号（前端据此显示
    「已完成（包含审核提醒）」），public_status 仍为已完成。"""
    task_id = await _seed(sessionmaker, orchestration=("completed_with_warnings", "completed"))
    service = TaskWorkspaceService(sessionmaker)
    workspace = await service.get_workspace(task_id)
    assert workspace.task.public_status == PUBLIC_STATUS_COMPLETED
    assert workspace.task.completed_with_warnings is True


# ---------------------------------------------------------------- regression：等待确认


@pytest.mark.asyncio
async def test_awaiting_stage5_all_positions_waiting_confirmation(sessionmaker) -> None:
    """用户验收：报告完成但等待 Stage5 human review（waiting_human +
    awaiting_stage5）→ 所有位置统一显示「等待确认」（不得继续显示待执行/已创建）。"""
    task_id = await _seed(
        sessionmaker,
        orchestration=("waiting_human", "awaiting_stage5"),
    )

    assert await _list_public_status(sessionmaker, task_id) == PUBLIC_STATUS_WAITING_CONFIRMATION
    assert (
        await _workspace_public_status(sessionmaker, task_id) == PUBLIC_STATUS_WAITING_CONFIRMATION
    )
    async with sessionmaker() as session:
        service = TaskService(ResearchTaskRepository(session), sessionmaker)
        fetched = await service.get_task(task_id)
    assert fetched.public_status == PUBLIC_STATUS_WAITING_CONFIRMATION


# ---------------------------------------------------------------- 其他状态


@pytest.mark.asyncio
async def test_no_orchestration_not_started(sessionmaker) -> None:
    """无 orchestration 的 pending task → 未开始（所有位置一致）。"""
    task_id = await _seed(sessionmaker)
    assert await _list_public_status(sessionmaker, task_id) == PUBLIC_STATUS_NOT_STARTED
    assert await _workspace_public_status(sessionmaker, task_id) == PUBLIC_STATUS_NOT_STARTED


@pytest.mark.asyncio
async def test_running_orchestration_in_progress(sessionmaker) -> None:
    task_id = await _seed(sessionmaker, orchestration=("running", "stage4"))
    assert await _list_public_status(sessionmaker, task_id) == PUBLIC_STATUS_IN_PROGRESS
    assert await _workspace_public_status(sessionmaker, task_id) == PUBLIC_STATUS_IN_PROGRESS


@pytest.mark.asyncio
async def test_failed_orchestration_failed(sessionmaker) -> None:
    task_id = await _seed(sessionmaker, orchestration=("failed", "stage4"))
    assert await _list_public_status(sessionmaker, task_id) == PUBLIC_STATUS_FAILED
    assert await _workspace_public_status(sessionmaker, task_id) == PUBLIC_STATUS_FAILED
