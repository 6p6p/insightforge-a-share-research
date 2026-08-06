"""Integration tests for research task persistence against real PostgreSQL.

Run explicitly with:
    conda run -n insightforge python -m pytest \
        -c backend/pyproject.toml backend/tests/integration -m integration -v
"""

import asyncio
import uuid
from datetime import date
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.research_task import ResearchTaskModel
from app.db.session import DatabaseManager
from app.domain.tasks import TaskStatus
from app.repositories.research_task_repository import ResearchTaskRepository
from app.schemas.task import TaskCreateRequest
from app.services.task_service import TaskService

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
async def session_factory(database):
    return database.session_factory()


@pytest_asyncio.fixture
async def session(session_factory):
    async with session_factory() as session:
        yield session


def _model(**overrides: object) -> ResearchTaskModel:
    defaults: dict = {
        "company_query": "600519",
        "research_start_date": date(2023, 1, 1),
        "research_end_date": date(2025, 12, 31),
        "modules": ["company_profile"],
        "questions": [],
        "include_relative_valuation": False,
        "require_plan_approval": True,
        "status": "pending",
        "current_stage": "created",
        "progress": 0,
    }
    defaults.update(overrides)
    return ResearchTaskModel(**defaults)


@pytest.mark.asyncio
async def test_research_tasks_table_exists(session) -> None:
    result = await session.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'research_tasks'"
        )
    )
    assert result.scalar_one_or_none() == "research_tasks"


@pytest.mark.asyncio
async def test_repository_create_and_get(session) -> None:
    repo = ResearchTaskRepository(session)
    task = _model()
    await repo.create(task)
    fetched = await repo.get_by_id(task.task_id)
    assert fetched is not None
    assert fetched.company_query == "600519"
    assert fetched.status == "pending"
    assert fetched.progress == 0
    await session.rollback()


@pytest.mark.asyncio
async def test_list_stable_ordering_and_total(session) -> None:
    repo = ResearchTaskRepository(session)
    first = _model()
    second = _model()
    third = _model(status="failed")
    for task in (first, second, third):
        await repo.create(task)

    rows, total = await repo.list_tasks(status=None, limit=10, offset=0)
    assert total == 3
    assert len(rows) == 3
    # created_at DESC, task_id DESC：同事务 created_at 相同，按 task_id 降序
    assert rows[0].task_id > rows[1].task_id

    failed_rows, failed_total = await repo.list_tasks(status=TaskStatus.FAILED, limit=10, offset=0)
    assert failed_total == 1
    assert failed_rows[0].task_id == third.task_id
    await session.rollback()


@pytest.mark.asyncio
async def test_idempotency_key_unique_constraint(session) -> None:
    repo = ResearchTaskRepository(session)
    key = str(uuid.uuid4())
    await repo.create(_model(idempotency_key=key, request_fingerprint="a" * 64))
    with pytest.raises(IntegrityError):
        await repo.create(_model(idempotency_key=key, request_fingerprint="b" * 64))
    await session.rollback()


@pytest.mark.asyncio
async def test_date_range_check_constraint(session) -> None:
    repo = ResearchTaskRepository(session)
    with pytest.raises(IntegrityError):
        await repo.create(
            _model(
                research_start_date=date(2025, 1, 1),
                research_end_date=date(2023, 1, 1),
            )
        )
    await session.rollback()


@pytest.mark.asyncio
async def test_progress_check_constraint(session) -> None:
    repo = ResearchTaskRepository(session)
    with pytest.raises(IntegrityError):
        await repo.create(_model(progress=150))
    await session.rollback()


@pytest.mark.asyncio
async def test_rollback_does_not_persist(session) -> None:
    repo = ResearchTaskRepository(session)
    task = _model()
    await repo.create(task)
    await session.rollback()
    assert await repo.get_by_id(task.task_id) is None


@pytest.mark.asyncio
async def test_idempotency_pair_check_constraint(session) -> None:
    repo = ResearchTaskRepository(session)
    with pytest.raises(IntegrityError):
        await repo.create(_model(idempotency_key="key-only"))
    await session.rollback()


def _request(**overrides: object) -> TaskCreateRequest:
    defaults: dict = {
        "company_query": "600519",
        "research_start_date": date(2023, 1, 1),
        "research_end_date": date(2025, 12, 31),
        "modules": ["company_profile"],
        "questions": [],
    }
    defaults.update(overrides)
    return TaskCreateRequest.model_validate(defaults)


@pytest.mark.asyncio
async def test_concurrent_idempotent_creation(session_factory) -> None:
    key = f"concurrent-{uuid.uuid4()}"
    request = _request()

    async def attempt() -> tuple[bool, UUID]:
        async with session_factory() as session:
            repository = ResearchTaskRepository(session)
            service = TaskService(repository)
            result = await service.create_task(request, key)
            await session.commit()
            return result.replayed, result.task.task_id

    results = await asyncio.gather(attempt(), attempt())

    task_ids = {task_id for _, task_id in results}
    replayed_flags = sorted(replayed for replayed, _ in results)
    assert len(task_ids) == 1
    assert replayed_flags == [False, True]

    async with session_factory() as cleanup:
        await cleanup.execute(
            text("DELETE FROM research_tasks WHERE idempotency_key = :key"),
            {"key": key},
        )
        await cleanup.commit()
