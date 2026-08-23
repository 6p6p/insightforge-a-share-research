"""Tests for the task service idempotency and coordination logic."""

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.errors import IdempotencyConflict, TaskHasDependentData, TaskNotFound
from app.db.models.research_task import ResearchTaskModel
from app.domain.tasks import TaskStatus
from app.schemas.task import TaskCreateRequest
from app.services.task_service import TaskService

pytestmark = pytest.mark.asyncio


class _FakeSession:
    def __init__(self) -> None:
        self.rolled_back = False

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        self.rolled_back = True


class _FakeDiag:
    def __init__(self, sqlstate: str, constraint_name: str) -> None:
        self.sqlstate = sqlstate
        self.constraint_name = constraint_name


class _FakeUniqueViolation(Exception):
    def __init__(self) -> None:
        self.diag = _FakeDiag("23505", "uq_research_tasks_idempotency_key")


class _FakeFkViolation(Exception):
    def __init__(self) -> None:
        self.diag = _FakeDiag("23503", "some_fk_constraint")


class FakeResearchTaskRepository:
    """In-memory repository; can simulate a late idempotency-key conflict."""

    def __init__(
        self,
        *,
        simulate_late_conflict: bool = False,
        simulate_fk_conflict: bool = False,
    ) -> None:
        self.session = _FakeSession()
        self.by_id: dict[UUID, ResearchTaskModel] = {}
        self.by_key: dict[str, ResearchTaskModel] = {}
        self.simulate_late_conflict = simulate_late_conflict
        self.simulate_fk_conflict = simulate_fk_conflict
        self.late_existing: ResearchTaskModel | None = None
        self.query_count = 0
        self.last_list: tuple[TaskStatus | None, int, int] | None = None

    async def create(self, task: ResearchTaskModel) -> ResearchTaskModel:
        if self.simulate_late_conflict:
            raise IntegrityError("stmt", {}, _FakeUniqueViolation())
        # 模拟真实 flush 回填的默认值
        task.task_id = task.task_id or uuid4()
        task.created_at = task.created_at or datetime.now(UTC)
        task.updated_at = task.updated_at or datetime.now(UTC)
        self.by_id[task.task_id] = task
        if task.idempotency_key:
            self.by_key[task.idempotency_key] = task
        return task

    async def get_by_id(self, task_id: UUID) -> ResearchTaskModel | None:
        return self.by_id.get(task_id)

    async def get_by_idempotency_key(self, key: str) -> ResearchTaskModel | None:
        self.query_count += 1
        if self.simulate_late_conflict and self.late_existing is not None and self.query_count >= 2:
            return self.late_existing
        return self.by_key.get(key)

    async def list_tasks(
        self,
        *,
        status: TaskStatus | None,
        limit: int,
        offset: int,
    ) -> tuple[list[ResearchTaskModel], int]:
        self.last_list = (status, limit, offset)
        rows = [t for t in self.by_id.values() if status is None or t.status == status.value]
        rows.sort(key=lambda t: (t.created_at, t.task_id), reverse=True)
        return rows[offset : offset + limit], len(rows)

    async def delete(self, task: ResearchTaskModel) -> None:
        if self.simulate_fk_conflict:
            raise IntegrityError("stmt", {}, _FakeFkViolation())
        self.by_id.pop(task.task_id, None)
        if task.idempotency_key:
            self.by_key.pop(task.idempotency_key, None)


def _request(**overrides: object) -> TaskCreateRequest:
    defaults: dict = {
        "company_query": "600519",
        "research_start_date": date(2023, 1, 1),
        "research_end_date": date(2025, 12, 31),
        "modules": ["company_profile", "financial"],
        "questions": ["公司收入增长主要由哪些因素驱动？"],
    }
    defaults.update(overrides)
    return TaskCreateRequest.model_validate(defaults)


def _model(**overrides: object) -> ResearchTaskModel:
    defaults: dict = {
        "task_id": uuid4(),
        "company_query": "600519",
        "research_start_date": date(2023, 1, 1),
        "research_end_date": date(2025, 12, 31),
        "modules": ["company_profile", "financial"],
        "questions": ["公司收入增长主要由哪些因素驱动？"],
        "include_relative_valuation": False,
        "require_plan_approval": True,
        "status": "pending",
        "current_stage": "created",
        "progress": 0,
        "idempotency_key": None,
        "request_fingerprint": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return ResearchTaskModel(**defaults)


async def test_create_without_key_creates_new() -> None:
    repo = FakeResearchTaskRepository()
    service = TaskService(repo)

    result = await service.create_task(_request(), None)

    assert result.replayed is False
    assert result.task.status.value == "pending"
    assert result.task.current_stage.value == "created"
    assert result.task.progress == 0
    assert len(repo.by_id) == 1


async def test_same_key_same_request_replays() -> None:
    repo = FakeResearchTaskRepository()
    service = TaskService(repo)
    key = "replay-key"
    request = _request()

    first = await service.create_task(request, key)
    second = await service.create_task(request, key)

    assert first.replayed is False
    assert second.replayed is True
    assert second.task.task_id == first.task.task_id
    assert len(repo.by_id) == 1


async def test_same_key_different_request_conflicts() -> None:
    repo = FakeResearchTaskRepository()
    service = TaskService(repo)
    key = "conflict-key"

    await service.create_task(_request(), key)
    with pytest.raises(IdempotencyConflict):
        await service.create_task(_request(company_query="000001"), key)


async def test_get_missing_task_raises_not_found() -> None:
    repo = FakeResearchTaskRepository()
    service = TaskService(repo)

    with pytest.raises(TaskNotFound):
        await service.get_task(uuid4())


async def test_get_existing_task() -> None:
    repo = FakeResearchTaskRepository()
    service = TaskService(repo)
    task = _model()
    repo.by_id[task.task_id] = task

    response = await service.get_task(task.task_id)

    assert response.task_id == task.task_id


async def test_list_passes_parameters() -> None:
    repo = FakeResearchTaskRepository()
    service = TaskService(repo)

    await service.list_tasks(status=TaskStatus.PENDING, limit=5, offset=10)

    assert repo.last_list == (TaskStatus.PENDING, 5, 10)


async def test_late_conflict_replays_after_rollback() -> None:
    repo = FakeResearchTaskRepository(simulate_late_conflict=True)
    service = TaskService(repo)
    key = "concurrent-key"
    request = _request()
    fingerprint = TaskService._fingerprint(request)
    repo.late_existing = _model(idempotency_key=key, request_fingerprint=fingerprint)

    result = await service.create_task(request, key)

    assert result.replayed is True
    assert repo.session.rolled_back is True


async def test_late_conflict_conflicts_on_different_fingerprint() -> None:
    repo = FakeResearchTaskRepository(simulate_late_conflict=True)
    service = TaskService(repo)
    key = "concurrent-key"
    other_fingerprint = "f" * 64
    repo.late_existing = _model(idempotency_key=key, request_fingerprint=other_fingerprint)

    with pytest.raises(IdempotencyConflict):
        await service.create_task(_request(), key)


async def test_delete_missing_task_raises_not_found() -> None:
    repo = FakeResearchTaskRepository()
    service = TaskService(repo)

    with pytest.raises(TaskNotFound):
        await service.delete_task(uuid4())


async def test_delete_existing_task_removes_row() -> None:
    repo = FakeResearchTaskRepository()
    service = TaskService(repo)
    task = _model()
    repo.by_id[task.task_id] = task

    await service.delete_task(task.task_id)

    assert task.task_id not in repo.by_id
    assert len(repo.by_id) == 0


async def test_delete_with_dependent_data_raises_conflict() -> None:
    repo = FakeResearchTaskRepository(simulate_fk_conflict=True)
    service = TaskService(repo)
    task = _model()
    repo.by_id[task.task_id] = task

    with pytest.raises(TaskHasDependentData):
        await service.delete_task(task.task_id)
