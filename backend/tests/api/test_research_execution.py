"""Tests for the Stage 6A research execution API.

覆盖三个新路由：
- `POST /tasks/{task_id}/execute`：202 / 404 / 409 / 422；
- `GET /tasks/{task_id}/workspace`：workspace projection / 404；
- `GET /tasks/{task_id}/events`：task 级 SSE 流、Last-Event-ID 续传、错误路径。

全部依赖注入 Fake：不触碰真实 PG / LLM。
"""

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest

from app.api.dependencies import (
    get_langgraph_checkpoint_manager,
    get_research_execution_service,
    get_task_service,
    get_task_workspace_service,
    get_workflow_service,
)
from app.core.errors import (
    ActiveWorkflowRunExists,
    MissingResearchQuestion,
    TaskNotFound,
)
from app.db.dependencies import get_database
from app.domain.tasks import WorkflowEventType
from app.main import create_app
from app.schemas.research_execution import ArtifactSummary, TaskWorkspaceResponse
from app.schemas.task import TaskResponse
from app.schemas.workflow import WorkflowEventResponse, WorkflowRunResponse
from app.vectorstore.dependencies import get_chroma


def _task_response(**overrides: object) -> TaskResponse:
    defaults: dict = {
        "task_id": uuid4(),
        "company_query": "600519",
        "research_start_date": date(2023, 1, 1),
        "research_end_date": date(2025, 12, 31),
        "modules": ["company_profile", "financial"],
        "questions": ["贵州茅台 2024 年盈利能力如何？"],
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


def _run_response(**overrides: object) -> WorkflowRunResponse:
    defaults: dict = {
        "run_id": uuid4(),
        "task_id": uuid4(),
        "thread_id": str(uuid4()),
        "graph_name": "stage4_analysis",
        "graph_version": "4.x",
        "status": "running",
        "started_at": None,
        "completed_at": None,
        "failed_at": None,
        "error_code": None,
        "error_message": None,
        "pending_action": None,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    defaults.update(overrides)
    return WorkflowRunResponse.model_validate(defaults)


def _event_response(**overrides: object) -> WorkflowEventResponse:
    defaults: dict = {
        "event_id": 1,
        "run_id": "00000000-0000-0000-0000-000000000001",
        "event_type": WorkflowEventType.RUN_CREATED,
        "node_name": None,
        "stage": "created",
        "progress": 0,
        "message": "研究执行已创建",
        "payload": {},
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    defaults.update(overrides)
    return WorkflowEventResponse.model_validate(defaults)


def _workspace_response(**overrides: object) -> TaskWorkspaceResponse:
    defaults: dict = {
        "task": _task_response(),
        "resolved_company": None,
        "current_run": None,
        "artifact_summary": ArtifactSummary(
            source_count=3,
            evidence_count=7,
            claim_count=2,
            report_count=1,
            review_issue_count=0,
        ),
    }
    defaults.update(overrides)
    return TaskWorkspaceResponse.model_validate(defaults)


def _execute_payload(**overrides: object) -> dict:
    defaults: dict = {
        "analysis_work_items": [
            {
                "item_id": "wi-1",
                "analysis_type": "business",
                "evidence_card_ids": [str(uuid4())],
            }
        ]
    }
    defaults.update(overrides)
    return defaults


class FakeTaskService:
    def __init__(self) -> None:
        self.get_error: Exception | None = None

    async def get_task(self, task_id: UUID) -> TaskResponse:
        if self.get_error is not None:
            raise self.get_error
        return _task_response(task_id=task_id)


class FakeWorkflowService:
    def __init__(self) -> None:
        self.events: list[WorkflowEventResponse] = []
        self.terminal = False
        self.last_after: tuple[UUID, int, int] | None = None

    async def list_events_after_for_task(self, task_id, after_event_id, limit=100) -> list:
        self.last_after = (task_id, after_event_id, limit)
        return [e for e in self.events if e.event_id > after_event_id][:limit]

    async def is_task_terminal(self, task_id: UUID) -> bool:
        return self.terminal


class FakeResearchExecutionService:
    def __init__(self) -> None:
        self.start_error: Exception | None = None
        self.start_result: WorkflowRunResponse | None = None
        self.start_calls: list[tuple[UUID, object]] = []
        self.running: set[UUID] = set()

    async def start(self, task_id: UUID, request) -> WorkflowRunResponse:
        self.start_calls.append((task_id, request))
        if self.start_error is not None:
            raise self.start_error
        if self.start_result is not None:
            return self.start_result
        return _run_response(task_id=task_id)

    def is_running(self, task_id: UUID) -> bool:
        return task_id in self.running


class FakeTaskWorkspaceService:
    def __init__(self) -> None:
        self.get_error: Exception | None = None
        self.get_result: TaskWorkspaceResponse | None = None

    async def get_workspace(self, task_id: UUID) -> TaskWorkspaceResponse:
        if self.get_error is not None:
            raise self.get_error
        if self.get_result is not None:
            return self.get_result
        return _workspace_response(task=_task_response(task_id=task_id))


@pytest.fixture
def fake_task_service() -> FakeTaskService:
    return FakeTaskService()


@pytest.fixture
def fake_workflow_service() -> FakeWorkflowService:
    return FakeWorkflowService()


@pytest.fixture
def fake_research_execution() -> FakeResearchExecutionService:
    return FakeResearchExecutionService()


@pytest.fixture
def fake_task_workspace_service() -> FakeTaskWorkspaceService:
    return FakeTaskWorkspaceService()


@pytest.fixture
def app(
    test_settings,
    fake_database,
    fake_chroma,
    fake_langgraph,
    fake_task_service,
    fake_workflow_service,
    fake_research_execution,
    fake_task_workspace_service,
):
    application = create_app(test_settings)
    application.dependency_overrides[get_database] = lambda: fake_database
    application.dependency_overrides[get_chroma] = lambda: fake_chroma
    application.dependency_overrides[get_langgraph_checkpoint_manager] = lambda: fake_langgraph
    application.dependency_overrides[get_task_service] = lambda: fake_task_service
    application.dependency_overrides[get_workflow_service] = lambda: fake_workflow_service
    application.dependency_overrides[get_research_execution_service] = lambda: (
        fake_research_execution
    )
    application.dependency_overrides[get_task_workspace_service] = lambda: (
        fake_task_workspace_service
    )
    return application


# ---------------------------------------------------------------- execute


def test_execute_returns_202(client, fake_research_execution) -> None:
    run = _run_response()
    fake_research_execution.start_result = run

    response = client.post(
        f"/api/v1/tasks/{run.task_id}/execute",
        json=_execute_payload(),
    )

    assert response.status_code == 202
    body = response.json()
    assert body["run_id"] == str(run.run_id)
    assert body["status"] == "running"
    assert fake_research_execution.start_calls[0][0] == run.task_id


def test_execute_missing_task_returns_404(client, fake_research_execution) -> None:
    fake_research_execution.start_error = TaskNotFound()

    response = client.post(
        f"/api/v1/tasks/{uuid4()}/execute",
        json=_execute_payload(),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "task_not_found"


def test_execute_active_run_conflict_returns_409(client, fake_research_execution) -> None:
    fake_research_execution.start_error = ActiveWorkflowRunExists()

    response = client.post(
        f"/api/v1/tasks/{uuid4()}/execute",
        json=_execute_payload(),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "active_workflow_run_exists"


def test_execute_missing_question_returns_422(client, fake_research_execution) -> None:
    fake_research_execution.start_error = MissingResearchQuestion()

    response = client.post(
        f"/api/v1/tasks/{uuid4()}/execute",
        json=_execute_payload(),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "missing_research_question"


def test_execute_empty_work_items_rejected_422(client, fake_research_execution) -> None:
    response = client.post(
        f"/api/v1/tasks/{uuid4()}/execute",
        json={"analysis_work_items": []},
    )

    assert response.status_code == 422
    assert fake_research_execution.start_calls == []


def test_execute_duplicate_item_ids_rejected_422(client, fake_research_execution) -> None:
    evidence = str(uuid4())
    response = client.post(
        f"/api/v1/tasks/{uuid4()}/execute",
        json=_execute_payload(
            analysis_work_items=[
                {
                    "item_id": "wi-1",
                    "analysis_type": "business",
                    "evidence_card_ids": [evidence],
                },
                {
                    "item_id": "wi-1",
                    "analysis_type": "risk",
                    "evidence_card_ids": [evidence],
                },
            ]
        ),
    )

    assert response.status_code == 422
    assert fake_research_execution.start_calls == []


def test_execute_invalid_analysis_type_rejected_422(client, fake_research_execution) -> None:
    response = client.post(
        f"/api/v1/tasks/{uuid4()}/execute",
        json={
            "analysis_work_items": [
                {"item_id": "wi-1", "analysis_type": "unknown", "evidence_card_ids": [str(uuid4())]}
            ]
        },
    )

    assert response.status_code == 422
    assert fake_research_execution.start_calls == []


# ---------------------------------------------------------------- workspace


def test_workspace_returns_projection(client, fake_task_workspace_service) -> None:
    workspace = _workspace_response()
    fake_task_workspace_service.get_result = workspace

    response = client.get(f"/api/v1/tasks/{workspace.task.task_id}/workspace")

    assert response.status_code == 200
    body = response.json()
    assert body["task"]["task_id"] == str(workspace.task.task_id)
    assert body["artifact_summary"]["source_count"] == 3
    assert body["artifact_summary"]["evidence_count"] == 7
    assert body["current_run"] is None


def test_workspace_includes_current_run(client, fake_task_workspace_service) -> None:
    task = _task_response()
    run = _run_response(task_id=task.task_id, status="waiting_human")
    fake_task_workspace_service.get_result = _workspace_response(task=task, current_run=run)

    response = client.get(f"/api/v1/tasks/{task.task_id}/workspace")

    assert response.status_code == 200
    body = response.json()
    assert body["current_run"]["status"] == "waiting_human"
    assert body["current_run"]["run_id"] == str(run.run_id)


def test_workspace_missing_task_returns_404(client, fake_task_workspace_service) -> None:
    fake_task_workspace_service.get_error = TaskNotFound()

    response = client.get(f"/api/v1/tasks/{uuid4()}/workspace")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "task_not_found"


# ---------------------------------------------------------------- task SSE


def test_task_events_streams_and_terminates(client, fake_workflow_service) -> None:
    run = _run_response(status="completed")
    fake_workflow_service.events = [
        _event_response(event_id=1, event_type=WorkflowEventType.RUN_CREATED),
        _event_response(
            event_id=2,
            event_type=WorkflowEventType.RUN_WAITING_HUMAN,
            stage="human_review",
            progress=70,
        ),
        _event_response(event_id=3, event_type=WorkflowEventType.RUN_COMPLETED),
    ]
    fake_workflow_service.terminal = True

    response = client.get(f"/api/v1/tasks/{run.task_id}/events")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["connection"] == "keep-alive"
    assert response.headers["x-accel-buffering"] == "no"
    assert "id: 1" in response.text
    assert "id: 2" in response.text
    assert "id: 3" in response.text
    assert "event: run_waiting_human" in response.text
    assert "event: run_completed" in response.text


def test_task_events_replay_from_cursor(client, fake_workflow_service) -> None:
    run = _run_response(status="completed")
    fake_workflow_service.events = [_event_response(event_id=i) for i in range(1, 6)]
    fake_workflow_service.terminal = True

    response = client.get(
        f"/api/v1/tasks/{run.task_id}/events",
        headers={"Last-Event-ID": "2"},
    )

    body = response.text
    for event_id in ("1", "2"):
        assert f"id: {event_id}" not in body
    for event_id in ("3", "4", "5"):
        assert f"id: {event_id}" in body
    ids = [line.split(": ")[1] for line in body.splitlines() if line.startswith("id: ")]
    assert ids == ["3", "4", "5"]


def test_task_events_invalid_last_event_id_returns_400(
    client, fake_workflow_service, fake_task_service
) -> None:
    response = client.get(
        f"/api/v1/tasks/{uuid4()}/events",
        headers={"Last-Event-ID": "abc"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_last_event_id"


def test_task_events_missing_task_returns_404(client, fake_task_service) -> None:
    fake_task_service.get_error = TaskNotFound()

    response = client.get(f"/api/v1/tasks/{uuid4()}/events")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "task_not_found"


# ---------------------------------------------------------------- openapi


def test_openapi_contains_research_execution_endpoints(client) -> None:
    schema = client.get("/openapi.json").json()
    assert "/api/v1/tasks/{task_id}/execute" in schema["paths"]
    assert "/api/v1/tasks/{task_id}/workspace" in schema["paths"]
    assert "/api/v1/tasks/{task_id}/events" in schema["paths"]
    assert "/api/v1/workflow-runs/{run_id}/actions" in schema["paths"]
