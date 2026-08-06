"""Tests for the workflow execution manager."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.schemas.workflow import WorkflowRunResponse
from app.workflows.execution_manager import WorkflowExecutionManager

pytestmark = pytest.mark.asyncio


def _run_response(task_id: UUID) -> WorkflowRunResponse:
    return WorkflowRunResponse.model_validate(
        {
            "run_id": uuid4(),
            "task_id": task_id,
            "thread_id": str(uuid4()),
            "graph_name": "research_workflow_simulation",
            "graph_version": "1b.1",
            "status": "pending",
            "started_at": None,
            "completed_at": None,
            "failed_at": None,
            "error_code": None,
            "error_message": None,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
    )


class FakeRunner:
    def __init__(
        self,
        execute_delay: float = 0.0,
        execute_error: Exception | None = None,
    ) -> None:
        self.execute_delay = execute_delay
        self.execute_error = execute_error
        self.created: list[WorkflowRunResponse] = []
        self.executed: set[UUID] = set()

    async def create_simulation_run(self, task_id: UUID) -> WorkflowRunResponse:
        run = _run_response(task_id)
        self.created.append(run)
        return run

    async def execute_simulation(self, run_id: UUID) -> dict:
        if self.execute_error is not None:
            raise self.execute_error
        await asyncio.sleep(self.execute_delay)
        self.executed.add(run_id)
        return {"simulation_complete": True}

    async def get_run(self, run_id: UUID) -> WorkflowRunResponse:
        return _run_response(uuid4())


async def test_start_creates_background_task() -> None:
    manager = WorkflowExecutionManager(FakeRunner(), shutdown_timeout_seconds=1)
    run = await manager.start_simulation(uuid4())
    assert run.run_id in manager._tasks
    await manager.close()


async def test_task_removed_after_completion() -> None:
    manager = WorkflowExecutionManager(FakeRunner(), shutdown_timeout_seconds=1)
    run = await manager.start_simulation(uuid4())
    for _ in range(20):
        if run.run_id not in manager._tasks:
            break
        await asyncio.sleep(0.01)
    assert run.run_id not in manager._tasks
    await manager.close()


async def test_task_exception_is_consumed() -> None:
    manager = WorkflowExecutionManager(
        FakeRunner(execute_error=ValueError("boom")), shutdown_timeout_seconds=1
    )
    run = await manager.start_simulation(uuid4())
    await asyncio.sleep(0.05)
    # done callback 消费了异常，不会出现 "Task exception was never retrieved"
    assert run.run_id not in manager._tasks
    await manager.close()


async def test_close_waits_for_running_task() -> None:
    runner = FakeRunner(execute_delay=0.05)
    manager = WorkflowExecutionManager(runner, shutdown_timeout_seconds=5)
    run = await manager.start_simulation(uuid4())
    await manager.close()
    assert run.run_id in runner.executed


async def test_close_cancels_overdue_task() -> None:
    runner = FakeRunner(execute_delay=10.0)
    manager = WorkflowExecutionManager(runner, shutdown_timeout_seconds=0.1)
    run = await manager.start_simulation(uuid4())
    await manager.close()
    assert run.run_id not in manager._tasks
    assert run.run_id not in runner.executed


async def test_close_is_idempotent() -> None:
    manager = WorkflowExecutionManager(FakeRunner(), shutdown_timeout_seconds=1)
    await manager.close()
    await manager.close()


async def test_start_after_close_rejected() -> None:
    manager = WorkflowExecutionManager(FakeRunner(), shutdown_timeout_seconds=1)
    await manager.close()
    with pytest.raises(RuntimeError):
        await manager.start_simulation(uuid4())
