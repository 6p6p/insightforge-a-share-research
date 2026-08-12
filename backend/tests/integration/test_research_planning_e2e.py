"""Research planning full-chain E2E (stage 7A.1 spec U).

真实 PostgreSQL + FakeResearchPlannerModel + Fake Stage4 分析模型 + 真实
LangGraph + PG Checkpointer，全程 **0 真实 DeepSeek**：

- **ready=true 全链**：Planner（create_plan）→ Deterministic Router
  （route_research_plan）→ Preparation（prepare_research）→ `ready_for_analysis`
  = True → 有效 `Stage4WorkflowRequest` → **实际 Stage4WorkflowRunner**
  （business / risk / financial / macro / valuation → Send fan-out → 真实
  Services → Synthesis）直到 `SynthesisResult`。验证：auto work plan 是真实可
  执行的（不是只有 schema 合法），run completed、1 SynthesisRun + 1 Result。
- **ready=false 不触发 Stage4**：plan 需要 `ps_ttm` comparison 但库中没有 →
  `ready_for_analysis` = False、`stage4_request` = None、**0 条 workflow_runs**
  （不伪造 readiness、不启动 Stage4）。
"""

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.research_planning.preparation import ResearchPreparationService
from app.research_planning.router import ResearchSourceRouter
from app.research_planning.service import ResearchPlanningService
from app.services.company_identity_service import CompanyIdentityService
from app.services.source_registry_service import SourceRegistryService
from app.stage4.runner import Stage4WorkflowRunner
from app.storage.raw_store import LocalRawArtifactStore
from app.workflows.checkpoint import LangGraphCheckpointManager
from tests.integration.test_research_planning_service import (
    _QUESTION,
    _cleanup,
    _plan_payload,
    _seed_research_task,
)
from tests.integration.test_stage4_workflow import (
    _build_deps,
    _good_models,
    _seed_worker_inputs,
    _synthesis_counts,
)
from tests.integration.test_valuation_claim_service import _seed_company
from tests.research_planning.fakes import FakeResearchPlannerModel

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()


# ---------------------------------------------------------------- env / helpers


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
async def env(tmp_path, sessionmaker) -> dict:
    raw_store = LocalRawArtifactStore(root=tmp_path / "raw", max_bytes=1024 * 1024)
    await _cleanup(sessionmaker)
    await SourceRegistryService(sessionmaker).seed_defaults()
    company_id = await _seed_company(sessionmaker, "600519")
    peer_company_ids = [await _seed_company(sessionmaker, f"6005{2 + i:02d}") for i in range(3)]
    task_id = await _seed_research_task(sessionmaker)
    yield {
        "sessionmaker": sessionmaker,
        "raw_store": raw_store,
        "company_id": company_id,
        "target_company_id": company_id,
        "peer_company_ids": peer_company_ids,
        "task_id": task_id,
    }
    await _cleanup(sessionmaker)


def _planner(sessionmaker, fake: FakeResearchPlannerModel) -> ResearchPlanningService:
    return ResearchPlanningService(sessionmaker, fake, CompanyIdentityService(sessionmaker))


def _preparation(sessionmaker, fake: FakeResearchPlannerModel) -> ResearchPreparationService:
    plan_service = _planner(sessionmaker, fake)
    return ResearchPreparationService(
        sessionmaker, plan_service, ResearchSourceRouter(sessionmaker, plan_service)
    )


async def _workflow_run_count(sessionmaker) -> int:
    async with sessionmaker() as session:
        return int((await session.execute(text("SELECT count(*) FROM workflow_runs"))).scalar_one())


# ---------------------------------------------------------------- E2E：ready=true 全链


async def test_e2e_planner_router_preparation_to_stage4_synthesis(
    env, monkeypatch, connection_uri
) -> None:
    """ready=true → 有效 Stage4 request → 真实 Stage4 runner → SynthesisResult。"""
    await _seed_worker_inputs(env, monkeypatch, research_question=_QUESTION)
    fake = FakeResearchPlannerModel(_plan_payload())
    preparation = _preparation(env["sessionmaker"], fake)
    plan_service = _planner(env["sessionmaker"], fake)
    plan_result = await plan_service.create_plan(env["task_id"])
    await ResearchSourceRouter(env["sessionmaker"], plan_service).route_research_plan(
        plan_result.research_plan_id
    )
    result = await preparation.prepare_research(plan_result.research_plan_id)

    assert result.ready_for_analysis is True
    assert result.missing_needs == ()
    req = result.stage4_request
    assert req is not None
    assert req.task_id == env["task_id"]
    assert req.company_id == env["company_id"]
    assert req.research_question == "分析贵州茅台的经营质量、主要风险和估值水平。"
    assert len(req.analysis_work_items) == 5

    # 真实 Stage4 runner：auto work plan 必须真实可执行（直到 SynthesisResult）。
    deps = _build_deps(env["sessionmaker"], _good_models())
    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        runner = Stage4WorkflowRunner(env["sessionmaker"], manager, deps)
        run = await runner.create_stage4_run(req)
        await runner.execute_stage4(run.run_id, req)
        final = await runner.get_run(run.run_id)
    finally:
        await manager.close()

    assert final.status.value == "completed"
    runs, results = await _synthesis_counts(env["sessionmaker"])
    assert runs == 1
    assert results == 1


# ---------------------------------------------------------------- E2E：ready=false 不触发 Stage4


async def test_e2e_missing_valuation_ready_false_no_stage4(env, monkeypatch) -> None:
    """需要 ps_ttm comparison 但没有 → ready=false、无 stage4_request、0 条 workflow_runs。"""
    await _seed_worker_inputs(env, monkeypatch, research_question=_QUESTION)
    fake = FakeResearchPlannerModel(
        _plan_payload(valuation_needs=[{"need_code": "ps_valuation", "metric_code": "ps_ttm"}])
    )
    plan_service = _planner(env["sessionmaker"], fake)
    preparation = ResearchPreparationService(
        env["sessionmaker"], plan_service, ResearchSourceRouter(env["sessionmaker"], plan_service)
    )
    plan_result = await plan_service.create_plan(env["task_id"])
    await ResearchSourceRouter(env["sessionmaker"], plan_service).route_research_plan(
        plan_result.research_plan_id
    )
    result = await preparation.prepare_research(plan_result.research_plan_id)

    assert result.ready_for_analysis is False
    assert result.stage4_request is None
    # 不触发任何 Stage4 run（无 workflow_runs 行）。
    assert await _workflow_run_count(env["sessionmaker"]) == 0
