"""Tests for the research task API endpoints."""

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest

from app.api.dependencies import get_task_service
from app.core.errors import IdempotencyConflict, TaskNotFound
from app.db.dependencies import get_database
from app.domain.tasks import TaskStatus
from app.main import create_app
from app.schemas.task import TaskCreateRequest, TaskListResponse, TaskResponse
from app.services.task_service import TaskCreationResult
from app.vectorstore.dependencies import get_chroma


def _payload(**overrides: object) -> dict:
    defaults: dict = {
        "company_query": "600519",
        "research_start_date": "2023-01-01",
        "research_end_date": "2025-12-31",
        "modules": ["company_profile", "financial"],
        "questions": [],
    }
    defaults.update(overrides)
    return defaults


def _task_response(**overrides: object) -> TaskResponse:
    defaults: dict = {
        "task_id": uuid4(),
        "company_query": "600519",
        "research_start_date": date(2023, 1, 1),
        "research_end_date": date(2025, 12, 31),
        "modules": ["company_profile", "financial"],
        "questions": [],
        "include_relative_valuation": False,
        "require_plan_approval": True,
        "status": "pending",
        "current_stage": "created",
        "progress": 0,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    defaults.update(overrides)
    return TaskResponse.model_validate(defaults)


class FakeTaskService:
    def __init__(self) -> None:
        self.create_result: TaskCreationResult | None = None
        self.create_error: Exception | None = None
        self.create_calls: list[tuple[TaskCreateRequest, str | None]] = []
        self.get_result: TaskResponse | None = None
        self.get_error: Exception | None = None
        self.list_result: TaskListResponse | None = None
        self.list_calls: list[tuple[TaskStatus | None, int, int]] = []

    async def create_task(
        self,
        request: TaskCreateRequest,
        idempotency_key: str | None,
    ) -> TaskCreationResult:
        self.create_calls.append((request, idempotency_key))
        if self.create_error is not None:
            raise self.create_error
        if self.create_result is not None:
            return self.create_result
        return TaskCreationResult(task=_task_response(), replayed=False)

    async def get_task(self, task_id: UUID) -> TaskResponse:
        if self.get_error is not None:
            raise self.get_error
        if self.get_result is not None:
            return self.get_result
        return _task_response(task_id=task_id)

    async def list_tasks(
        self,
        status: TaskStatus | None,
        limit: int,
        offset: int,
    ) -> TaskListResponse:
        self.list_calls.append((status, limit, offset))
        if self.list_result is not None:
            return self.list_result
        return TaskListResponse(items=[_task_response()], total=1, limit=limit, offset=offset)


@pytest.fixture
def fake_task_service() -> FakeTaskService:
    return FakeTaskService()


@pytest.fixture
def app(test_settings, fake_database, fake_chroma, fake_task_service):
    application = create_app(test_settings)
    application.dependency_overrides[get_database] = lambda: fake_database
    application.dependency_overrides[get_chroma] = lambda: fake_chroma
    application.dependency_overrides[get_task_service] = lambda: fake_task_service
    return application


def test_create_task_returns_201(client, fake_task_service) -> None:
    fake_task_service.create_result = TaskCreationResult(task=_task_response(), replayed=False)

    response = client.post("/api/v1/tasks", json=_payload())

    assert response.status_code == 201
    assert response.json()["status"] == "pending"
    assert response.headers["Idempotent-Replayed"] == "false"


def test_create_replayed_returns_200_with_header(client, fake_task_service) -> None:
    fake_task_service.create_result = TaskCreationResult(task=_task_response(), replayed=True)

    response = client.post(
        "/api/v1/tasks",
        json=_payload(),
        headers={"Idempotency-Key": "replay-key"},
    )

    assert response.status_code == 200
    assert response.headers["Idempotent-Replayed"] == "true"


def test_create_conflict_returns_409(client, fake_task_service) -> None:
    fake_task_service.create_error = IdempotencyConflict()

    response = client.post(
        "/api/v1/tasks",
        json=_payload(),
        headers={"Idempotency-Key": "conflict-key"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_conflict"


def test_invalid_idempotency_key_too_long_returns_400(client, fake_task_service) -> None:
    response = client.post(
        "/api/v1/tasks",
        json=_payload(),
        headers={"Idempotency-Key": "x" * 200},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_idempotency_key"
    assert fake_task_service.create_calls == []


def test_invalid_idempotency_key_non_printable_returns_400(client, fake_task_service) -> None:
    response = client.post(
        "/api/v1/tasks",
        json=_payload(),
        headers={"Idempotency-Key": "ab\x01cd"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_idempotency_key"


def test_get_task_returns_200(client, fake_task_service) -> None:
    task = _task_response()
    fake_task_service.get_result = task

    response = client.get(f"/api/v1/tasks/{task.task_id}")

    assert response.status_code == 200
    assert response.json()["task_id"] == str(task.task_id)


def test_get_missing_task_returns_unified_error(client, fake_task_service) -> None:
    fake_task_service.get_error = TaskNotFound()

    response = client.get(f"/api/v1/tasks/{uuid4()}")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "task_not_found"
    assert body["error"]["message"]
    assert body["error"]["request_id"]


def test_list_tasks_with_status_and_pagination(client, fake_task_service) -> None:
    fake_task_service.list_result = TaskListResponse(
        items=[_task_response()],
        total=1,
        limit=5,
        offset=10,
    )

    response = client.get("/api/v1/tasks", params={"status": "pending", "limit": 5, "offset": 10})

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert fake_task_service.list_calls[-1] == (TaskStatus.PENDING, 5, 10)


def test_list_tasks_rejects_out_of_range_params(client, fake_task_service) -> None:
    assert client.get("/api/v1/tasks", params={"limit": 0}).status_code == 422
    assert client.get("/api/v1/tasks", params={"limit": 101}).status_code == 422
    assert client.get("/api/v1/tasks", params={"offset": -1}).status_code == 422


def test_openapi_contains_task_endpoints(client) -> None:
    schema = client.get("/openapi.json").json()
    assert "/api/v1/tasks" in schema["paths"]
    assert "/api/v1/tasks/{task_id}" in schema["paths"]


def test_health_unaffected_by_task_api(client) -> None:
    assert client.get("/api/v1/health/live").status_code == 200
    assert client.get("/api/v1/health/ready").status_code == 200
