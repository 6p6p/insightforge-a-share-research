"""Minimal research orchestration API tests（7A.2B.2 spec U/V + Gate C/E，0 DB / 0 real LLM）。

route 只做协议层 dispatch：本文件 override `get_research_orchestration_service`
为 Fake service，断言 URL → service 方法 → response 投影 + HTTP 状态码（新建
201 / 已调度 202 / 已存在 200）；业务语义由
`tests/research_orchestration/test_service.py` 覆盖（route 不执行业务）。
"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi.testclient import TestClient

from app.api.dependencies import get_research_orchestration_service
from app.research_orchestration.contracts import (
    ORCHESTRATION_SCHEMA_VERSION,
    ORCHESTRATOR_NAME,
    ORCHESTRATOR_VERSION,
)
from app.research_orchestration.errors import (
    ResearchOrchestrationNotFound,
    ResearchOrchestrationRetryRequired,
)
from app.research_orchestration.service import (
    OrchestrationStartOutcome,
    ResearchOrchestrationResult,
)

_OID = UUID("00000000-0000-0000-0000-000000000001")
_TASK_ID = UUID("00000000-0000-0000-0000-000000000002")


def _result(**overrides):
    base = dict(
        orchestration_id=_OID,
        task_id=_TASK_ID,
        research_plan_id=None,
        orchestration_schema_version=ORCHESTRATION_SCHEMA_VERSION,
        orchestrator_name=ORCHESTRATOR_NAME,
        orchestrator_version=ORCHESTRATOR_VERSION,
        status="waiting_human",
        current_phase="awaiting_stage5",
        input_fingerprint="f" * 64,
        started_at=None,
        completed_at=None,
        error_code=None,
        error_message=None,
        created_at=datetime(2026, 8, 12, tzinfo=UTC),
        attempt_no=1,
        retry_of_orchestration_id=None,
        replayed=False,
    )
    base.update(overrides)
    return ResearchOrchestrationResult(**base)


class FakeOrchestrationService:
    """记录 dispatch 的 fake service（route 只投影 result + 状态码）。"""

    def __init__(self) -> None:
        self.start_calls: list[UUID] = []
        self.get_current_calls: list[UUID] = []
        self.get_calls: list[UUID] = []
        self.action_calls: list[tuple] = []
        self.retry_calls: list[UUID] = []
        self.resume_calls: list[UUID] = []
        self.result = _result()
        self.raise_not_found = False
        self.raise_retry_required = False
        self.outcome = OrchestrationStartOutcome(
            orchestration=self.result, created=True, scheduled=True
        )

    async def prepare_orchestration_start(self, task_id):
        self.start_calls.append(task_id)
        if self.raise_retry_required:
            raise ResearchOrchestrationRetryRequired()
        return self.outcome

    async def get_current_orchestration(self, task_id):
        self.get_current_calls.append(task_id)
        return self.result

    async def get_orchestration(self, orchestration_id):
        self.get_calls.append(orchestration_id)
        if self.raise_not_found:
            raise ResearchOrchestrationNotFound()
        return self.result

    async def act_on_orchestration(self, orchestration_id, action, comment=None):
        self.action_calls.append((orchestration_id, action, comment))
        return self.result

    async def retry_and_schedule(self, orchestration_id):
        self.retry_calls.append(orchestration_id)
        return self.result

    async def resume_after_source_acquisition(self, orchestration_id):
        self.resume_calls.append(orchestration_id)
        return self.result


def _client(app, fake) -> TestClient:
    app.dependency_overrides[get_research_orchestration_service] = lambda: fake
    return TestClient(app)


# ------------------------------------------------------------------ create / read


def test_post_create_orchestration_dispatches_start(app) -> None:
    """全新创建 → 201，route 调用 `prepare_orchestration_start`（Gate C）。"""
    fake = FakeOrchestrationService()
    with _client(app, fake) as client:
        response = client.post(f"/api/v1/tasks/{_TASK_ID}/orchestrations")
    assert response.status_code == 201
    assert fake.start_calls == [_TASK_ID]
    body = response.json()
    assert body["orchestration_id"] == str(_OID)
    assert body["task_id"] == str(_TASK_ID)
    assert body["status"] == "waiting_human"
    assert body["current_phase"] == "awaiting_stage5"


def test_post_create_scheduled_existing_returns_202(app) -> None:
    """已存在 active=pending 且本进程调度后台运行 → 202。"""
    fake = FakeOrchestrationService()
    fake.outcome = OrchestrationStartOutcome(
        orchestration=fake.result, created=False, scheduled=True
    )
    with _client(app, fake) as client:
        response = client.post(f"/api/v1/tasks/{_TASK_ID}/orchestrations")
    assert response.status_code == 202
    assert fake.start_calls == [_TASK_ID]


def test_post_create_existing_no_schedule_returns_200(app) -> None:
    """已存在且未调度（running / waiting_human / completed）→ 200。"""
    fake = FakeOrchestrationService()
    fake.outcome = OrchestrationStartOutcome(
        orchestration=fake.result, created=False, scheduled=False
    )
    with _client(app, fake) as client:
        response = client.post(f"/api/v1/tasks/{_TASK_ID}/orchestrations")
    assert response.status_code == 200
    assert fake.start_calls == [_TASK_ID]


def test_post_create_latest_failed_returns_409(app) -> None:
    """无 active、latest=failed/cancelled → 409 retry_required（不偷偷回 attempt1）。"""
    fake = FakeOrchestrationService()
    fake.raise_retry_required = True
    with _client(app, fake) as client:
        response = client.post(f"/api/v1/tasks/{_TASK_ID}/orchestrations")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "research_orchestration_retry_required"
    assert fake.start_calls == [_TASK_ID]


def test_get_current_orchestration(app) -> None:
    fake = FakeOrchestrationService()
    with _client(app, fake) as client:
        response = client.get(f"/api/v1/tasks/{_TASK_ID}/orchestrations/current")
    assert response.status_code == 200
    assert fake.get_current_calls == [_TASK_ID]
    assert response.json()["orchestration_id"] == str(_OID)


def test_get_orchestration_by_id(app) -> None:
    fake = FakeOrchestrationService()
    with _client(app, fake) as client:
        response = client.get(f"/api/v1/research-orchestrations/{_OID}")
    assert response.status_code == 200
    assert fake.get_calls == [_OID]
    assert response.json()["status"] == "waiting_human"


def test_get_orchestration_not_found_returns_404(app) -> None:
    fake = FakeOrchestrationService()
    fake.raise_not_found = True
    with _client(app, fake) as client:
        response = client.get(f"/api/v1/research-orchestrations/{_OID}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "research_orchestration_not_found"


# ------------------------------------------------------------------ actions


def test_actions_human_approve_dispatch(app) -> None:
    fake = FakeOrchestrationService()
    with _client(app, fake) as client:
        response = client.post(
            f"/api/v1/research-orchestrations/{_OID}/actions",
            json={"action": "approve", "comment": "同意"},
        )
    assert response.status_code == 200
    assert fake.action_calls == [(_OID, "approve", "同意")]
    assert fake.retry_calls == []


def test_actions_rewrite_and_research_dispatch(app) -> None:
    for action in ("rewrite", "research"):
        fake = FakeOrchestrationService()
        with _client(app, fake) as client:
            response = client.post(
                f"/api/v1/research-orchestrations/{_OID}/actions",
                json={"action": action},
            )
        assert response.status_code == 200
        assert fake.action_calls == [(_OID, action, None)]


def test_actions_cancel_dispatch(app) -> None:
    fake = FakeOrchestrationService()
    with _client(app, fake) as client:
        response = client.post(
            f"/api/v1/research-orchestrations/{_OID}/actions",
            json={"action": "cancel"},
        )
    assert response.status_code == 200
    assert fake.action_calls == [(_OID, "cancel", None)]
    assert fake.retry_calls == []


def test_actions_retry_dispatch(app) -> None:
    fake = FakeOrchestrationService()
    with _client(app, fake) as client:
        response = client.post(
            f"/api/v1/research-orchestrations/{_OID}/actions",
            json={"action": "retry"},
        )
    assert response.status_code == 200
    assert fake.retry_calls == [_OID]
    assert fake.action_calls == []


def test_actions_unknown_action_rejected_422(app) -> None:
    fake = FakeOrchestrationService()
    with _client(app, fake) as client:
        response = client.post(
            f"/api/v1/research-orchestrations/{_OID}/actions",
            json={"action": "teleport"},
        )
    assert response.status_code == 422
    assert fake.action_calls == []
    assert fake.retry_calls == []


# ---------------------- resume after source acquisition（7A Product Gate spec J/K/L）


def test_resume_source_acquisition_dispatch(app) -> None:
    """补资料后 → POST /resume-source-acquisition → service dispatch → 200 投影。"""
    fake = FakeOrchestrationService()
    fake.result = _result(status="waiting_human", current_phase="waiting_manual")
    with _client(app, fake) as client:
        response = client.post(
            f"/api/v1/research-orchestrations/{_OID}/resume-source-acquisition"
        )
    assert response.status_code == 200
    assert fake.resume_calls == [_OID]
    body = response.json()
    assert body["orchestration_id"] == str(_OID)
    assert body["status"] == "waiting_human"
    assert body["current_phase"] == "waiting_manual"
