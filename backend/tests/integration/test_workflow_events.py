"""Integration tests for workflow events and background execution."""

import asyncio
from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.config import get_settings
from app.core.errors import ActiveWorkflowRunExists
from app.core.runtime import configure_asyncio_runtime
from app.db.models.research_task import ResearchTaskModel
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.repositories.research_task_repository import ResearchTaskRepository
from app.repositories.workflow_event_repository import WorkflowEventRepository
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
    }
    defaults.update(overrides)
    return ResearchTaskModel(**defaults)


async def _events_for(sessionmaker, run_id) -> list:
    async with sessionmaker() as session:
        repo = WorkflowEventRepository(session)
        events = await repo.list_after(run_id=run_id, after_event_id=0, limit=500)
    return events


@pytest.mark.asyncio
async def test_workflow_events_background_execution(database, connection_uri, sessionmaker) -> None:
    # 1. workflow_events 表存在
    async with sessionmaker() as session:
        result = await session.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'workflow_events'"
            )
        )
        assert result.scalar_one_or_none() == "workflow_events"

    # 2. 创建任务并启动真实模拟 run
    async with sessionmaker() as session:
        task = _task()
        await ResearchTaskRepository(session).create(task)
        await session.commit()
        task_id = task.task_id

    checkpoint = LangGraphCheckpointManager(connection_uri)
    await checkpoint.setup()
    runner = WorkflowRunner(sessionmaker, checkpoint)
    manager = WorkflowExecutionManager(runner, shutdown_timeout_seconds=10)

    run = await manager.start_simulation(task_id)
    for _ in range(50):
        if run.run_id not in manager._tasks:
            break
        await asyncio.sleep(0.1)

    # 3. 最终 completed
    run_resp = await manager.get_run(run.run_id)
    assert run_resp.status.value == "completed"

    # 4. 事件顺序准确
    events = await _events_for(sessionmaker, run.run_id)
    types = [event.event_type for event in events]
    assert types == [
        "run_created",
        "run_started",
        "node_completed",
        "node_completed",
        "node_completed",
        "run_completed",
    ]

    # 5. event_id 严格递增
    ids = [event.event_id for event in events]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)

    # 6. list_after 断点回放（从 run_started 之后）
    replayed = await _events_for_delayed(sessionmaker, run.run_id, ids[1])
    assert [event.event_id for event in replayed] == ids[2:]

    # 7. 相同任务并发启动只产生一个 active run
    pending_run = await runner.create_simulation_run(task_id)
    with pytest.raises(ActiveWorkflowRunExists):
        await runner.create_simulation_run(task_id)
    async with sessionmaker() as session:
        await session.execute(
            text("DELETE FROM workflow_events WHERE run_id = :r"),
            {"r": pending_run.run_id},
        )
        await session.execute(
            text("DELETE FROM workflow_runs WHERE run_id = :r"),
            {"r": pending_run.run_id},
        )
        await session.commit()

    # 8. Checkpoint 最终状态存在
    graph = build_research_workflow(await checkpoint.get_checkpointer())
    state = await graph.aget_state({"configurable": {"thread_id": run.thread_id}})
    assert state.values["simulation_complete"] is True

    # 9. ResearchTask 仍 pending/created/0
    async with sessionmaker() as session:
        fetched = await ResearchTaskRepository(session).get_by_id(task_id)
        assert fetched.status == "pending"
        assert fetched.current_stage == "created"
        assert fetched.progress == 0

    # 10. close 后无活动 asyncio Task
    await manager.close()
    assert len(manager._tasks) == 0
    await checkpoint.close()

    # 11. 精确清理测试数据
    async with sessionmaker() as session:
        await session.execute(
            text("DELETE FROM workflow_events WHERE run_id = :r"),
            {"r": run.run_id},
        )
        await session.execute(
            text("DELETE FROM workflow_runs WHERE run_id = :r"),
            {"r": run.run_id},
        )
        await session.execute(
            text("DELETE FROM research_tasks WHERE task_id = :t"),
            {"t": task_id},
        )
        await session.commit()


async def _events_for_delayed(sessionmaker, run_id, after_id) -> list:
    async with sessionmaker() as session:
        repo = WorkflowEventRepository(session)
        events = await repo.list_after(run_id=run_id, after_event_id=after_id, limit=500)
    return events
