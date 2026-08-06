"""Integration tests for workflow interrupts, resume, cancel, retry and recovery."""

import asyncio
from datetime import date
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.errors import WorkflowRunAlreadyFinished, WorkflowRunAlreadyStarted
from app.core.runtime import configure_asyncio_runtime
from app.db.models.research_task import ResearchTaskModel
from app.db.models.workflow_run import WorkflowRunModel
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.domain.tasks import HumanActionType
from app.repositories.human_action_repository import HumanActionRepository
from app.repositories.research_task_repository import ResearchTaskRepository
from app.repositories.workflow_event_repository import WorkflowEventRepository
from app.repositories.workflow_run_repository import WorkflowRunRepository
from app.schemas.workflow import WorkflowRunResponse
from app.services.workflow_recovery_service import WorkflowRecoveryService
from app.workflows.checkpoint import LangGraphCheckpointManager
from app.workflows.execution_manager import WorkflowExecutionManager
from app.workflows.graph import build_research_workflow
from app.workflows.runner import WorkflowRunner

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
def connection_uri() -> str:
    return to_postgres_connection_uri(get_settings().database_url)


@pytest_asyncio.fixture
async def sessionmaker(database):
    return database.session_factory()


def _task(**overrides: object) -> ResearchTaskModel:
    defaults: dict = {
        "company_query": "600519",
        "research_start_date": date(2023, 1, 1),
        "research_end_date": date(2025, 12, 31),
        "modules": ["company_profile"],
        "questions": [],
        "status": "pending",
        "current_stage": "created",
        "progress": 0,
        "require_plan_approval": True,
    }
    defaults.update(overrides)
    return ResearchTaskModel(**defaults)


def _run(task_id, **overrides: object) -> WorkflowRunModel:
    defaults: dict = {
        "run_id": uuid4(),
        "task_id": task_id,
        "thread_id": str(uuid4()),
        "graph_name": "research_workflow_simulation",
        "graph_version": "1d.1",
        "status": "pending",
    }
    defaults.update(overrides)
    return WorkflowRunModel(**defaults)


async def _events(sessionmaker, run_id) -> list:
    async with sessionmaker() as session:
        repo = WorkflowEventRepository(session)
        events = await repo.list_after(run_id=run_id, after_event_id=0, limit=500)
    return events


async def _wait_status(sessionmaker, manager, run_id, target: str) -> WorkflowRunResponse:
    for _ in range(80):
        run = await manager.get_run(run_id)
        if run.status.value == target:
            return run
        await asyncio.sleep(0.1)
    raise AssertionError(f"run never reached {target}; last={run.status.value}")


@pytest.mark.asyncio
async def test_interrupt_resume_across_manager_restart(
    database, connection_uri, sessionmaker
) -> None:
    async with sessionmaker() as session:
        task = _task()
        await ResearchTaskRepository(session).create(task)
        await session.commit()
        task_id = task.task_id

    checkpoint = LangGraphCheckpointManager(connection_uri)
    await checkpoint.setup()
    runner = WorkflowRunner(sessionmaker, checkpoint)
    manager = WorkflowExecutionManager(runner, 5, sessionmaker)

    run = await manager.start_simulation(task_id)
    waiting = await _wait_status(sessionmaker, manager, run.run_id, "waiting_human")
    assert waiting.pending_action == "plan_approval"

    # Checkpoint 含 interrupt
    graph = build_research_workflow(await checkpoint.get_checkpointer())
    state = await graph.aget_state({"configurable": {"thread_id": run.thread_id}})
    assert any(task_item.interrupts for task_item in state.tasks)

    # 关闭 Manager（模拟进程重启）
    await manager.close()

    # 新 Manager 用同一 run/thread 恢复
    checkpoint2 = LangGraphCheckpointManager(connection_uri)
    runner2 = WorkflowRunner(sessionmaker, checkpoint2)
    manager2 = WorkflowExecutionManager(runner2, 5, sessionmaker)
    await manager2.resume_simulation(run.run_id, HumanActionType.APPROVE_PLAN)
    completed = await _wait_status(sessionmaker, manager2, run.run_id, "completed")
    assert completed.thread_id == run.thread_id
    await manager2.close()
    await checkpoint.close()
    await checkpoint2.close()

    # HumanAction 只有一条
    async with sessionmaker() as session:
        count = await HumanActionRepository(session).count_for_run(run.run_id)
    assert count == 1

    # 事件顺序
    events = await _events(sessionmaker, run.run_id)
    types = [event.event_type for event in events]
    assert types[0] == "run_created"
    assert "run_waiting_human" in types
    assert "run_resumed" in types
    assert types[-1] == "run_completed"

    # ResearchTask 不变
    async with sessionmaker() as session:
        fetched = await ResearchTaskRepository(session).get_by_id(task_id)
        assert fetched.status == "pending"
        assert fetched.current_stage == "created"
        assert fetched.progress == 0

    # 清理
    async with sessionmaker() as session:
        await session.execute(
            text("DELETE FROM workflow_events WHERE run_id = :r"), {"r": run.run_id}
        )
        await session.execute(
            text("DELETE FROM workflow_runs WHERE run_id = :r"), {"r": run.run_id}
        )
        await session.execute(text("DELETE FROM research_tasks WHERE task_id = :t"), {"t": task_id})
        await session.commit()


@pytest.mark.asyncio
async def test_cancel_waiting_human_and_retry(database, connection_uri, sessionmaker) -> None:
    async with sessionmaker() as session:
        task = _task()
        await ResearchTaskRepository(session).create(task)
        await session.commit()
        task_id = task.task_id

    checkpoint = LangGraphCheckpointManager(connection_uri)
    await checkpoint.setup()
    runner = WorkflowRunner(sessionmaker, checkpoint)
    manager = WorkflowExecutionManager(runner, 5, sessionmaker)

    run = await manager.start_simulation(task_id)
    await _wait_status(sessionmaker, manager, run.run_id, "waiting_human")

    cancelled = await manager.cancel_run(run.run_id)
    assert cancelled.status.value == "cancelled"
    assert cancelled.pending_action is None

    events = await _events(sessionmaker, run.run_id)
    assert events[-1].event_type == "run_cancelled"

    # retry 产生新 run/thread
    new_run = await manager.retry_run(run.run_id)
    assert new_run.run_id != run.run_id
    assert new_run.thread_id != run.thread_id

    # 清理（含 retry 的新 run）
    async with sessionmaker() as session:
        for rid in (run.run_id, new_run.run_id):
            await session.execute(text("DELETE FROM workflow_events WHERE run_id = :r"), {"r": rid})
            await session.execute(text("DELETE FROM workflow_runs WHERE run_id = :r"), {"r": rid})
        await session.execute(text("DELETE FROM research_tasks WHERE task_id = :t"), {"t": task_id})
        await session.commit()
    await manager.close()
    await checkpoint.close()


@pytest.mark.asyncio
async def test_reconcile_fails_orphaned_keeps_waiting_human(database, sessionmaker) -> None:
    async with sessionmaker() as session:
        task_pending = _task()
        task_waiting = _task()
        await ResearchTaskRepository(session).create(task_pending)
        await ResearchTaskRepository(session).create(task_waiting)
        run_repo = WorkflowRunRepository(session)
        pending_run = _run(task_pending.task_id, status="pending")
        waiting_run = _run(
            task_waiting.task_id,
            status="waiting_human",
            pending_action="plan_approval",
        )
        await run_repo.create(pending_run)
        await run_repo.create(waiting_run)
        await session.commit()
        pending_task_id = task_pending.task_id
        waiting_task_id = task_waiting.task_id

    service = WorkflowRecoveryService(sessionmaker)
    result = await service.reconcile_orphaned_runs()
    assert result.marked_failed == 1

    async with sessionmaker() as session:
        run_repo = WorkflowRunRepository(session)
        pending_after = await run_repo.get_by_id(pending_run.run_id)
        waiting_after = await run_repo.get_by_id(waiting_run.run_id)
    assert pending_after.status == "failed"
    assert pending_after.error_code == "worker_restarted"
    assert waiting_after.status == "waiting_human"

    # 重复 reconcile 幂等
    result2 = await service.reconcile_orphaned_runs()
    assert result2.marked_failed == 0

    async with sessionmaker() as session:
        for rid in (pending_run.run_id, waiting_run.run_id):
            await session.execute(text("DELETE FROM workflow_events WHERE run_id = :r"), {"r": rid})
            await session.execute(text("DELETE FROM workflow_runs WHERE run_id = :r"), {"r": rid})
        for tid in (pending_task_id, waiting_task_id):
            await session.execute(text("DELETE FROM research_tasks WHERE task_id = :t"), {"t": tid})
        await session.commit()


async def _approve(database, connection_uri, run_id):
    checkpoint = LangGraphCheckpointManager(connection_uri)
    runner = WorkflowRunner(database.session_factory(), checkpoint)
    manager = WorkflowExecutionManager(runner, 5, database.session_factory())
    try:
        return await manager.resume_simulation(run_id, HumanActionType.APPROVE_PLAN)
    finally:
        await manager.close()
        await checkpoint.close()


@pytest.mark.asyncio
async def test_concurrent_approve_only_one_accepts(database, connection_uri, sessionmaker) -> None:
    async with sessionmaker() as session:
        task = _task()
        await ResearchTaskRepository(session).create(task)
        await session.commit()
        task_id = task.task_id

    checkpoint = LangGraphCheckpointManager(connection_uri)
    await checkpoint.setup()
    runner = WorkflowRunner(sessionmaker, checkpoint)
    manager = WorkflowExecutionManager(runner, 5, sessionmaker)
    run = await manager.start_simulation(task_id)
    await _wait_status(sessionmaker, manager, run.run_id, "waiting_human")
    await manager.close()
    await checkpoint.close()

    results = await asyncio.gather(
        _approve(database, connection_uri, run.run_id),
        _approve(database, connection_uri, run.run_id),
        return_exceptions=True,
    )
    ok = [r for r in results if not isinstance(r, Exception)]
    errors = [r for r in results if isinstance(r, Exception)]
    assert len(ok) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], (WorkflowRunAlreadyStarted, WorkflowRunAlreadyFinished))

    checkpoint2 = LangGraphCheckpointManager(connection_uri)
    runner2 = WorkflowRunner(sessionmaker, checkpoint2)
    manager2 = WorkflowExecutionManager(runner2, 5, sessionmaker)
    try:
        await _wait_status(sessionmaker, manager2, run.run_id, "completed")
    finally:
        await manager2.close()
        await checkpoint2.close()

    async with sessionmaker() as session:
        human_count = await HumanActionRepository(session).count_for_run(run.run_id)
        events = await WorkflowEventRepository(session).list_after(
            run_id=run.run_id, after_event_id=0, limit=500
        )
    assert human_count == 1
    types = [event.event_type for event in events]
    assert types.count("run_resumed") == 1
    assert types.count("run_completed") == 1

    checkpoint3 = LangGraphCheckpointManager(connection_uri)
    try:
        graph = build_research_workflow(await checkpoint3.get_checkpointer())
        state = await graph.aget_state({"configurable": {"thread_id": run.thread_id}})
        assert state.values["plan_approved"] is True
    finally:
        await checkpoint3.close()

    async with sessionmaker() as session:
        await session.execute(
            text("DELETE FROM human_actions WHERE run_id = :r"), {"r": run.run_id}
        )
        await session.execute(
            text("DELETE FROM workflow_events WHERE run_id = :r"), {"r": run.run_id}
        )
        await session.execute(
            text("DELETE FROM workflow_runs WHERE run_id = :r"), {"r": run.run_id}
        )
        await session.execute(text("DELETE FROM research_tasks WHERE task_id = :t"), {"t": task_id})
        await session.commit()


@pytest.mark.asyncio
async def test_waiting_human_blocks_second_active_run(database, sessionmaker) -> None:
    async with sessionmaker() as session:
        task = _task()
        await ResearchTaskRepository(session).create(task)
        run_repo = WorkflowRunRepository(session)
        waiting = _run(task.task_id, status="waiting_human", pending_action="plan_approval")
        await run_repo.create(waiting)
        await session.commit()
        task_id = task.task_id

    async with sessionmaker() as session:
        run_repo = WorkflowRunRepository(session)
        active = await run_repo.get_active_for_task(task_id)
        assert active is not None
        assert active.status == "waiting_human"

    async with sessionmaker() as session:
        run_repo = WorkflowRunRepository(session)
        with pytest.raises(IntegrityError):
            await run_repo.create(_run(task_id, status="pending"))
        await session.rollback()

    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM workflow_runs WHERE task_id = :t"), {"t": task_id})
        await session.execute(text("DELETE FROM research_tasks WHERE task_id = :t"), {"t": task_id})
        await session.commit()


@pytest.mark.asyncio
async def test_pending_action_consistency_check(database, sessionmaker) -> None:
    cases = [
        ("waiting_human", None, True),
        ("running", "plan_approval", True),
        ("completed", "plan_approval", True),
        ("waiting_human", "plan_approval", False),
        ("running", None, False),
        ("completed", None, False),
    ]
    for status, pending, should_fail in cases:
        async with sessionmaker() as session:
            task = _task()
            await ResearchTaskRepository(session).create(task)
            run_repo = WorkflowRunRepository(session)
            run = _run(task.task_id, status=status, pending_action=pending)
            if should_fail:
                with pytest.raises(IntegrityError):
                    await run_repo.create(run)
                await session.rollback()
            else:
                await run_repo.create(run)
                await session.commit()
            await session.execute(
                text("DELETE FROM workflow_runs WHERE task_id = :t"), {"t": task.task_id}
            )
            await session.execute(
                text("DELETE FROM research_tasks WHERE task_id = :t"), {"t": task.task_id}
            )
            await session.commit()
