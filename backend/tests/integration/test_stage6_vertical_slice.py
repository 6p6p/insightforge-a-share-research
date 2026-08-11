"""Stage 6A vertical slice integration E2E（spec C/D/E/G）。

真实 PostgreSQL + 真实 LangGraph（AsyncPostgresSaver）+ Fake LLM models，
全程**零真实 DeepSeek**。Web 全链路 HTTP 走真实 app：

    POST /tasks → POST /tasks/{id}/execute（202，Stage4 run）
      → 后台链 Stage4（Fake analysis models，5 类 work item）
      → SynthesisResult → Stage5（Fake draft/audit/revision models）
      → human_review interrupt
    → workspace 轮询到 waiting_human → POST /workflow-runs/{id}/actions approve
    → workspace 轮询到 completed → task 级 SSE 读到 run_waiting_human / run_completed

另验证 active-run 不变式：已有 active run 时重复 execute → 409。

Stage4 复用 test_stage4_workflow 的 5 类 work item 种子（business/risk/financial/
macro/valuation 各 1 条 Claim，匹配 `_synthesis_output(5)`，避免
Stage4InsufficientClaims）。httpx ASGI transport 缓冲整条流，因此「观察
waiting_human」用 workspace 轮询；SSE 在 run completed 后自然终止，直接整读。
"""

import asyncio
import time
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from app.api.dependencies import get_research_execution_service
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.main import create_app
from app.services.company_identity_service import CompanyIdentityService
from app.services.research_execution_service import ResearchExecutionService
from app.services.source_registry_service import SourceRegistryService
from app.stage4.runner import Stage4WorkflowRunner
from app.stage5.runner import Stage5WorkflowRunner
from app.storage.raw_store import LocalRawArtifactStore
from app.workflows.checkpoint import LangGraphCheckpointManager
from tests.audit.fakes import FakeAuditModel
from tests.draft_section.fakes import FakeDraftSectionModel, valid_decision_for
from tests.integration.test_report_audit_service import human_review_decision
from tests.integration.test_revision_service import _cleanup_with_revisions
from tests.integration.test_stage4_workflow import _build_deps as _stage4_deps
from tests.integration.test_stage4_workflow import (
    _good_models,
    _request,
    _seed_worker_inputs,
)
from tests.integration.test_stage5_workflow import _stage5_deps
from tests.integration.test_valuation_claim_service import _seed_company
from tests.revision.fakes import FakeRevisionWriterModel

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_TASK_PAYLOAD = {
    "company_query": "600519",
    "research_start_date": "2023-01-01",
    "research_end_date": "2026-08-10",
    "modules": ["company_profile", "financial"],
    "questions": ["贵州茅台2026年营收与估值是否合理？"],
    "require_plan_approval": True,
}


def _execute_payload(analysis_work_items: list[dict]) -> dict:
    return {"analysis_work_items": analysis_work_items}


async def _wait_for_workspace(
    client: httpx.AsyncClient,
    task_id: str,
    predicate,
    timeout: float = 60.0,
) -> dict:
    """轮询 task workspace 直到 predicate(workspace body) 为真。"""
    deadline = time.monotonic() + timeout
    while True:
        response = await client.get(f"/api/v1/tasks/{task_id}/workspace")
        assert response.status_code == 200, response.text
        body = response.json()
        if predicate(body):
            return body
        if time.monotonic() > deadline:
            raise AssertionError(f"workspace 未在 {timeout}s 内满足条件: {body}")
        await asyncio.sleep(0.25)


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
async def sessionmaker(database):
    return database.session_factory()


@pytest_asyncio.fixture
async def connection_uri() -> str:
    return to_postgres_connection_uri(get_settings().database_url)


@pytest_asyncio.fixture
async def app_ctx(tmp_path, sessionmaker, connection_uri) -> dict:
    raw_store = LocalRawArtifactStore(root=tmp_path / "raw", max_bytes=1024 * 1024)
    await _cleanup_with_revisions(sessionmaker)
    await SourceRegistryService(sessionmaker).seed_defaults()

    checkpoint = LangGraphCheckpointManager(connection_uri)
    await checkpoint.setup()
    execution = ResearchExecutionService(
        sessionmaker=sessionmaker,
        checkpoint_manager=checkpoint,
        company_identity=CompanyIdentityService(sessionmaker),
        stage4_runner_factory=lambda: Stage4WorkflowRunner(
            sessionmaker, checkpoint, _stage4_deps(sessionmaker, _good_models())
        ),
        stage5_runner_factory=lambda: Stage5WorkflowRunner(
            sessionmaker,
            checkpoint,
            _stage5_deps(
                sessionmaker,
                draft_model=FakeDraftSectionModel(decision_factory=valid_decision_for),
                audit_model=FakeAuditModel(decision_factory=human_review_decision),
                revision_model=FakeRevisionWriterModel(),
            ),
        ),
    )

    app = create_app(get_settings())
    app.dependency_overrides[get_research_execution_service] = lambda: execution
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield {
                "client": client,
                "sessionmaker": sessionmaker,
                "checkpoint": checkpoint,
                "execution": execution,
                "raw_store": raw_store,
            }
            await execution.close()
    await checkpoint.close()
    await _cleanup_with_revisions(sessionmaker)


async def _create_task(app_ctx: dict) -> dict:
    response = await app_ctx["client"].post("/api/v1/tasks", json=_TASK_PAYLOAD)
    assert response.status_code == 201, response.text
    return response.json()


async def _seed_full_worker_env(app_ctx: dict, task_id: UUID, monkeypatch) -> tuple[dict, dict]:
    """seed company + 3 peers + 5 类 worker 输入。

    返回 `(env, ids)`：`env` 供 `_request` 定位 task/company，`ids` 是各 work item
    引用的真实 evidence / calculation / comparison ID。
    """
    company_id = await _seed_company(app_ctx["sessionmaker"], "600519")
    peers = [await _seed_company(app_ctx["sessionmaker"], f"6005{2 + i:02d}") for i in range(3)]
    env = {
        "task_id": task_id,
        "sessionmaker": app_ctx["sessionmaker"],
        "raw_store": app_ctx["raw_store"],
        "company_id": company_id,
        "target_company_id": company_id,
        "peer_company_ids": peers,
    }
    ids = await _seed_worker_inputs(env, monkeypatch)
    return env, ids


def _full_work_items(env: dict, ids: dict) -> list[dict]:
    """从 test_stage4_workflow._request 提取 5 类 work item（JSON-safe）。"""
    request = _request(env, ids)
    return [item.model_dump(mode="json") for item in request.analysis_work_items]


async def test_vertical_slice_happy_path(app_ctx, monkeypatch) -> None:
    client = app_ctx["client"]
    task = await _create_task(app_ctx)
    task_id = UUID(task["task_id"])
    env, ids = await _seed_full_worker_env(app_ctx, task_id, monkeypatch)
    work_items = _full_work_items(env, ids)

    # 1. 启动真实研究执行 → 202 Stage4 run。
    response = await client.post(
        f"/api/v1/tasks/{task_id}/execute", json=_execute_payload(work_items)
    )
    assert response.status_code == 202, response.text
    stage4_run_id = response.json()["run_id"]
    assert response.json()["graph_name"] == "stage4_analysis"

    # 2. 后台链推进 → Stage5 human_review interrupt → workspace current_run=waiting_human。
    workspace = await _wait_for_workspace(
        client,
        task_id,
        lambda b: bool(b["current_run"] and b["current_run"]["status"] == "waiting_human"),
    )
    stage5_run_id = workspace["current_run"]["run_id"]
    assert stage5_run_id != stage4_run_id

    # 3. human approve → finalize → run completed。
    response = await client.post(
        f"/api/v1/workflow-runs/{stage5_run_id}/actions",
        json={"action_type": "approve", "comment": "审核通过"},
    )
    assert response.status_code == 202, response.text
    assert response.json()["run"]["status"] == "completed"

    workspace = await _wait_for_workspace(
        client,
        task_id,
        lambda b: bool(b["current_run"] and b["current_run"]["status"] == "completed"),
    )
    assert workspace["resolved_company"]["security_code"] == "600519"
    assert workspace["artifact_summary"]["evidence_count"] >= 1
    assert workspace["artifact_summary"]["claim_count"] >= 1
    assert workspace["artifact_summary"]["report_count"] >= 1

    # 4. task 级 SSE：run completed 后自然终止，且包含关键事件。
    response = await client.get(f"/api/v1/tasks/{task_id}/events")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    body = response.text
    assert "event: run_waiting_human" in body
    assert "event: run_completed" in body
    assert "id: " in body


async def test_execute_conflicts_with_active_run_409(app_ctx, monkeypatch) -> None:
    client = app_ctx["client"]
    task = await _create_task(app_ctx)
    task_id = task["task_id"]
    await _seed_company(app_ctx["sessionmaker"], "600519")

    # 预置一个 active（pending）run → 重复 execute 必须 409 active_workflow_run_exists。
    async with app_ctx["sessionmaker"]() as session:
        await session.execute(
            text(
                "INSERT INTO workflow_runs "
                "(run_id, task_id, thread_id, graph_name, graph_version, status, "
                "created_at, updated_at) "
                "VALUES (:rid, :tid, :thr, 'stage4_analysis', '4.x', 'pending', now(), now())"
            ).bindparams(rid=uuid4(), tid=UUID(task_id), thr=str(uuid4()))
        )
        await session.commit()

    response = await client.post(
        f"/api/v1/tasks/{task_id}/execute",
        json=_execute_payload(
            [{"item_id": "biz", "analysis_type": "business", "evidence_card_ids": [str(uuid4())]}]
        ),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "active_workflow_run_exists"


async def test_workspace_available_before_any_run(app_ctx) -> None:
    """未启动执行时 workspace 也能渲染：current_run=None、无计数、公司已解析。"""
    client = app_ctx["client"]
    task = await _create_task(app_ctx)
    await _seed_company(app_ctx["sessionmaker"], "600519")

    response = await client.get(f"/api/v1/tasks/{task['task_id']}/workspace")

    assert response.status_code == 200
    body = response.json()
    assert body["current_run"] is None
    assert body["resolved_company"]["security_code"] == "600519"
    assert body["artifact_summary"]["source_count"] == 0
