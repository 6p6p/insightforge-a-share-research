"""Tests for the workflow API endpoints."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.api.dependencies import (
    get_langgraph_checkpoint_manager,
    get_research_execution_service,
    get_workflow_execution_manager,
    get_workflow_service,
)
from app.core.errors import WorkflowRunAlreadyFinished, WorkflowRunNotFound
from app.db.dependencies import get_database
from app.domain.tasks import WorkflowEventType
from app.main import create_app
from app.schemas.workflow import WorkflowEventResponse, WorkflowRunResponse
from app.stage5.contracts import STAGE5_GRAPH_NAME
from app.vectorstore.dependencies import get_chroma


def _run_response(**overrides: object) -> WorkflowRunResponse:
    defaults: dict = {
        "run_id": uuid4(),
        "task_id": uuid4(),
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
        "message": "工作流运行已创建",
        "payload": {},
        "created_at": "2026-01-01T00:00:00Z",
    }
    defaults.update(overrides)
    return WorkflowEventResponse.model_validate(defaults)


class FakeExecutionManager:
    def __init__(self) -> None:
        self.start_error: Exception | None = None
        self.start_result: WorkflowRunResponse | None = None
        self.started_task_ids: list[UUID] = []
        self.resume_result: WorkflowRunResponse | None = None
        self.cancel_result: WorkflowRunResponse | None = None
        self.cancel_error: Exception | None = None
        self.retry_result: WorkflowRunResponse | None = None

    async def start_simulation(self, task_id: UUID) -> WorkflowRunResponse:
        self.started_task_ids.append(task_id)
        if self.start_error is not None:
            raise self.start_error
        if self.start_result is not None:
            return self.start_result
        return _run_response(task_id=task_id)

    async def resume_simulation(self, run_id: UUID, action_type) -> WorkflowRunResponse:
        if self.resume_result is not None:
            return self.resume_result
        return _run_response(run_id=run_id, status="running")

    async def cancel_run(self, run_id: UUID) -> WorkflowRunResponse:
        if self.cancel_error is not None:
            raise self.cancel_error
        if self.cancel_result is not None:
            return self.cancel_result
        return _run_response(run_id=run_id, status="cancelled")

    async def retry_run(self, run_id: UUID) -> WorkflowRunResponse:
        if self.retry_result is not None:
            return self.retry_result
        return _run_response(status="pending")

    async def get_run(self, run_id: UUID) -> WorkflowRunResponse:
        return _run_response(run_id=run_id)


class FakeWorkflowService:
    def __init__(self) -> None:
        self.run: WorkflowRunResponse | None = None
        self.run_error: Exception | None = None
        self.events: list[WorkflowEventResponse] = []
        self.terminal = False
        self.last_after: tuple[UUID, int, int] | None = None

    async def get_run(self, run_id: UUID) -> WorkflowRunResponse:
        if self.run_error is not None:
            raise self.run_error
        return self.run or _run_response(run_id=run_id)

    async def list_events_after(self, run_id, after_event_id, limit=100) -> list:
        self.last_after = (run_id, after_event_id, limit)
        return [e for e in self.events if e.event_id > after_event_id][:limit]

    async def is_terminal(self, run_id: UUID) -> bool:
        return self.terminal


class FakeResearchExecutionService:
    """Stage 5 真实研究 run 的 human action 假实现（仅 actions 路由用到）。"""

    def __init__(self) -> None:
        self.resume_result: WorkflowRunResponse | None = None
        self.resume_error: Exception | None = None
        self.resume_calls: list[tuple[UUID, str, str | None]] = []
        self.cancel_result: WorkflowRunResponse | None = None
        self.cancel_error: Exception | None = None
        self.cancel_calls: list[UUID] = []

    async def resume_human(
        self, run_id: UUID, decision: str, comment: str | None = None
    ) -> WorkflowRunResponse:
        self.resume_calls.append((run_id, decision, comment))
        if self.resume_error is not None:
            raise self.resume_error
        if self.resume_result is not None:
            return self.resume_result
        return _run_response(run_id=run_id, graph_name=STAGE5_GRAPH_NAME, status="running")

    async def cancel(self, run_id: UUID) -> WorkflowRunResponse:
        self.cancel_calls.append(run_id)
        if self.cancel_error is not None:
            raise self.cancel_error
        if self.cancel_result is not None:
            return self.cancel_result
        return _run_response(run_id=run_id, graph_name=STAGE5_GRAPH_NAME, status="cancelled")


@pytest.fixture
def fake_execution_manager() -> FakeExecutionManager:
    return FakeExecutionManager()


@pytest.fixture
def fake_workflow_service() -> FakeWorkflowService:
    return FakeWorkflowService()


@pytest.fixture
def fake_research_execution() -> FakeResearchExecutionService:
    return FakeResearchExecutionService()


@pytest.fixture
def app(
    test_settings,
    fake_database,
    fake_chroma,
    fake_langgraph,
    fake_execution_manager,
    fake_workflow_service,
    fake_research_execution,
):
    application = create_app(test_settings)
    application.dependency_overrides[get_database] = lambda: fake_database
    application.dependency_overrides[get_chroma] = lambda: fake_chroma
    application.dependency_overrides[get_langgraph_checkpoint_manager] = lambda: fake_langgraph
    application.dependency_overrides[get_workflow_execution_manager] = lambda: (
        fake_execution_manager
    )
    application.dependency_overrides[get_workflow_service] = lambda: fake_workflow_service
    application.dependency_overrides[get_research_execution_service] = lambda: (
        fake_research_execution
    )
    return application


def test_start_run_returns_202(client, fake_execution_manager) -> None:
    run = _run_response(status="pending")
    fake_execution_manager.start_result = run

    response = client.post(f"/api/v1/tasks/{run.task_id}/runs")

    assert response.status_code == 202
    assert response.json()["status"] == "pending"
    assert fake_execution_manager.started_task_ids == [run.task_id]


def test_start_run_missing_task_returns_404(client, fake_execution_manager) -> None:
    fake_execution_manager.start_error = WorkflowRunNotFound()

    response = client.post(f"/api/v1/tasks/{uuid4()}/runs")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "workflow_run_not_found"


def test_get_run_returns_200(client, fake_workflow_service) -> None:
    run = _run_response(status="completed")
    fake_workflow_service.run = run

    response = client.get(f"/api/v1/workflow-runs/{run.run_id}")

    assert response.status_code == 200
    assert response.json()["run_id"] == str(run.run_id)


def test_get_missing_run_returns_404(client, fake_workflow_service) -> None:
    fake_workflow_service.run_error = WorkflowRunNotFound()

    response = client.get(f"/api/v1/workflow-runs/{uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "workflow_run_not_found"


def test_invalid_last_event_id_returns_400(client, fake_workflow_service) -> None:
    fake_workflow_service.terminal = True
    run = _run_response()
    fake_workflow_service.run = run

    response = client.get(
        f"/api/v1/workflow-runs/{run.run_id}/events",
        headers={"Last-Event-ID": "abc"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_last_event_id"


def test_sse_streams_events_and_headers(client, fake_workflow_service) -> None:
    run = _run_response()
    fake_workflow_service.run = run
    fake_workflow_service.events = [
        _event_response(event_id=1, event_type=WorkflowEventType.RUN_CREATED),
        _event_response(
            event_id=2,
            event_type=WorkflowEventType.NODE_COMPLETED,
            node_name="load_task_context",
        ),
    ]
    fake_workflow_service.terminal = True

    response = client.get(f"/api/v1/workflow-runs/{run.run_id}/events")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["connection"] == "keep-alive"
    assert response.headers["x-accel-buffering"] == "no"
    assert "id: 1" in response.text
    assert "id: 2" in response.text
    assert "event: run_created" in response.text
    assert "event: node_completed" in response.text


def test_sse_mid_stream_replay_returns_only_newer_events(client, fake_workflow_service) -> None:
    run = _run_response()
    fake_workflow_service.run = run
    fake_workflow_service.events = [_event_response(event_id=i) for i in range(1, 7)]
    fake_workflow_service.terminal = True

    response = client.get(
        f"/api/v1/workflow-runs/{run.run_id}/events",
        headers={"Last-Event-ID": "3"},
    )

    body = response.text
    for event_id in ("1", "2", "3"):
        assert f"id: {event_id}" not in body
    for event_id in ("4", "5", "6"):
        assert f"id: {event_id}" in body
    ids = [line.split(": ")[1] for line in body.splitlines() if line.startswith("id: ")]
    assert ids == ["4", "5", "6"]


def test_openapi_contains_workflow_endpoints(client) -> None:
    schema = client.get("/openapi.json").json()
    assert "/api/v1/tasks/{task_id}/runs" in schema["paths"]
    assert "/api/v1/workflow-runs/{run_id}" in schema["paths"]
    assert "/api/v1/workflow-runs/{run_id}/events" in schema["paths"]
    assert "/api/v1/workflow-runs/{run_id}/actions" in schema["paths"]


def test_approve_plan_returns_202(client, fake_execution_manager) -> None:
    run = _run_response(status="running")
    fake_execution_manager.resume_result = run

    response = client.post(
        f"/api/v1/workflow-runs/{run.run_id}/actions",
        json={"action_type": "approve_plan"},
    )

    assert response.status_code == 202
    assert response.json()["run"]["status"] == "running"


def test_cancel_returns_202(client, fake_execution_manager) -> None:
    run = _run_response(status="cancelled")
    fake_execution_manager.cancel_result = run

    response = client.post(
        f"/api/v1/workflow-runs/{run.run_id}/actions",
        json={"action_type": "cancel"},
    )

    assert response.status_code == 202
    assert response.json()["run"]["status"] == "cancelled"


def test_retry_returns_new_run(client, fake_execution_manager) -> None:
    new_run = _run_response(status="pending")
    fake_execution_manager.retry_result = new_run

    response = client.post(
        f"/api/v1/workflow-runs/{uuid4()}/actions",
        json={"action_type": "retry"},
    )

    assert response.status_code == 202
    assert response.json()["run"]["run_id"] == str(new_run.run_id)


def test_action_terminal_rejected_returns_409(client, fake_execution_manager) -> None:
    fake_execution_manager.cancel_error = WorkflowRunAlreadyFinished()

    response = client.post(
        f"/api/v1/workflow-runs/{uuid4()}/actions",
        json={"action_type": "cancel"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "workflow_run_already_finished"


def _stage5_run(**overrides: object) -> WorkflowRunResponse:
    return _run_response(graph_name=STAGE5_GRAPH_NAME, **overrides)


def test_stage5_approve_dispatches_resume_human(
    client, fake_workflow_service, fake_research_execution
) -> None:
    run = _stage5_run(status="running")
    fake_workflow_service.run = run
    fake_research_execution.resume_result = run

    response = client.post(
        f"/api/v1/workflow-runs/{run.run_id}/actions",
        json={"action_type": "approve"},
    )

    assert response.status_code == 202
    assert response.json()["run"]["status"] == "running"
    assert fake_research_execution.resume_calls == [(run.run_id, "approve", None)]


def test_stage5_rewrite_forwards_comment(
    client, fake_workflow_service, fake_research_execution
) -> None:
    run = _stage5_run(status="running")
    fake_workflow_service.run = run
    fake_research_execution.resume_result = run

    response = client.post(
        f"/api/v1/workflow-runs/{run.run_id}/actions",
        json={"action_type": "rewrite", "comment": "细化估值假设"},
    )

    assert response.status_code == 202
    assert fake_research_execution.resume_calls == [(run.run_id, "rewrite", "细化估值假设")]


def test_stage5_cancel_dispatches_research_cancel(
    client, fake_workflow_service, fake_research_execution
) -> None:
    run = _stage5_run(status="running")
    fake_workflow_service.run = run
    fake_research_execution.cancel_result = _stage5_run(status="cancelled")

    response = client.post(
        f"/api/v1/workflow-runs/{run.run_id}/actions",
        json={"action_type": "cancel"},
    )

    assert response.status_code == 202
    assert response.json()["run"]["status"] == "cancelled"
    assert fake_research_execution.cancel_calls == [run.run_id]
    assert fake_research_execution.resume_calls == []


def test_stage5_rejects_simulation_only_action(client, fake_workflow_service) -> None:
    run = _stage5_run(status="running")
    fake_workflow_service.run = run

    response = client.post(
        f"/api/v1/workflow-runs/{run.run_id}/actions",
        json={"action_type": "approve_plan"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "workflow_action_invalid"


def test_stage5_resume_terminal_rejected(
    client, fake_workflow_service, fake_research_execution
) -> None:
    run = _stage5_run(status="completed")
    fake_workflow_service.run = run
    fake_research_execution.resume_error = WorkflowRunAlreadyFinished()

    response = client.post(
        f"/api/v1/workflow-runs/{run.run_id}/actions",
        json={"action_type": "approve"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "workflow_run_already_finished"
