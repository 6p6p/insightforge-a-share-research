"""Integration tests for LangGraph workflow with PostgreSQL checkpointer."""

import uuid
from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.research_task import ResearchTaskModel
from app.db.models.workflow_run import WorkflowRunModel
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.repositories.research_task_repository import ResearchTaskRepository
from app.repositories.workflow_run_repository import WorkflowRunRepository
from app.workflows.checkpoint import LangGraphCheckpointManager
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


def _run(task_id, **overrides: object) -> WorkflowRunModel:
    defaults: dict = {
        "run_id": uuid.uuid4(),
        "task_id": task_id,
        "thread_id": str(uuid.uuid4()),
        "graph_name": "research_workflow_simulation",
        "graph_version": "1b.1",
        "status": "pending",
    }
    defaults.update(overrides)
    return WorkflowRunModel(**defaults)


@pytest.mark.asyncio
async def test_setup_is_reentrant_and_vendor_tables_exist(
    database, connection_uri, sessionmaker
) -> None:
    manager = LangGraphCheckpointManager(connection_uri)
    try:
        await manager.setup()
        await manager.setup()
    finally:
        await manager.close()

    async with sessionmaker() as session:
        result = await session.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name LIKE 'checkpoint%' "
                "ORDER BY table_name"
            )
        )
        tables = [row[0] for row in result]
    assert len(tables) >= 2


@pytest.mark.asyncio
async def test_workflow_run_repository_create_get(database, sessionmaker) -> None:
    task = _task()
    async with sessionmaker() as session:
        task_repo = ResearchTaskRepository(session)
        await task_repo.create(task)
        run_repo = WorkflowRunRepository(session)
        run = _run(task.task_id)
        await run_repo.create(run)
        await session.commit()

        fetched = await run_repo.get_by_id(run.run_id)
        assert fetched is not None
        assert fetched.thread_id == run.thread_id
        assert fetched.status == "pending"

        await session.execute(
            text("DELETE FROM workflow_runs WHERE run_id = :r"), {"r": run.run_id}
        )
        await session.execute(
            text("DELETE FROM research_tasks WHERE task_id = :t"), {"t": task.task_id}
        )
        await session.commit()


@pytest.mark.asyncio
async def test_one_active_run_per_task(database, sessionmaker) -> None:
    task = _task()
    async with sessionmaker() as session:
        task_repo = ResearchTaskRepository(session)
        await task_repo.create(task)
        await session.commit()
        task_id = task.task_id

        run_repo = WorkflowRunRepository(session)
        first = _run(task.task_id, status="pending")
        await run_repo.create(first)
        await session.commit()

        second = _run(task.task_id, status="running")
        with pytest.raises(IntegrityError):
            await run_repo.create(second)
        await session.rollback()
        # flush 失败后的 session 状态不可靠，用独立 session 清理
    async with sessionmaker() as cleanup:
        await cleanup.execute(text("DELETE FROM workflow_runs WHERE task_id = :t"), {"t": task_id})
        await cleanup.execute(
            text("DELETE FROM research_tasks WHERE task_id = :t2"), {"t2": task_id}
        )
        await cleanup.commit()


@pytest.mark.asyncio
async def test_full_simulation_persists_and_recovers(
    database, connection_uri, sessionmaker
) -> None:
    async with sessionmaker() as session:
        task = _task()
        await ResearchTaskRepository(session).create(task)
        await session.commit()
        task_id = task.task_id

    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    runner = WorkflowRunner(sessionmaker, manager)
    try:
        run, result = await runner.create_and_execute(task_id)
    finally:
        await manager.close()

    assert run.status.value == "completed"
    assert result["simulation_complete"] is True
    assert run.thread_id == str(run.run_id)

    # ResearchTask 必须保持 pending/created/0
    async with sessionmaker() as session:
        fetched = await ResearchTaskRepository(session).get_by_id(task_id)
        assert fetched is not None
        assert fetched.status == "pending"
        assert fetched.current_stage == "created"
        assert fetched.progress == 0

    config = {"configurable": {"thread_id": run.thread_id}}

    # 读取最终状态与历史（Manager 1）
    manager1 = LangGraphCheckpointManager(connection_uri)
    try:
        graph = build_research_workflow(await manager1.get_checkpointer())
        state = await graph.aget_state(config)
        assert state.values["simulation_complete"] is True
        history = [snapshot async for snapshot in graph.aget_state_history(config)]
        assert len(history) >= 3
    finally:
        await manager1.close()

    # 新 Manager 重新读取同一 thread_id 的最终状态
    manager2 = LangGraphCheckpointManager(connection_uri)
    try:
        graph2 = build_research_workflow(await manager2.get_checkpointer())
        state2 = await graph2.aget_state(config)
        assert state2.values["simulation_complete"] is True
    finally:
        await manager2.close()

    # 不创建业务表
    async with sessionmaker() as session:
        result = await session.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name IN "
                "('sources','evidence','claims','reports','report')"
            )
        )
        assert result.scalar_one_or_none() is None

    # 精确清理测试数据
    async with sessionmaker() as session:
        await session.execute(
            text("DELETE FROM workflow_runs WHERE run_id = :r"), {"r": run.run_id}
        )
        await session.execute(text("DELETE FROM research_tasks WHERE task_id = :t"), {"t": task_id})
        await session.commit()
