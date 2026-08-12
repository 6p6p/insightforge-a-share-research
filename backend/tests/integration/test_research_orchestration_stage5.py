"""Top-level research orchestration Stage5 全链集成测试（7A.2B.2 spec W Cases 1-7）。

真实 PostgreSQL + 真实 LangGraph（PG Checkpointer / AsyncPostgresSaver）+ 真实
`research_orchestration_runs` / `research_orchestration_child_runs` /
`workflow_runs` 表 + 真实 Stage4 runner + 真实 Synthesis + 真实 Stage5 runner；
plan/router 真实（FakeResearchPlannerModel），prepare/fulfill 注入可控 Fake，
Stage4/Stage5 全部 Fake models。全程**零真实 DeepSeek**。

Concentrated Cases 1-7（spec W）：
1. Task → orchestration → Stage4 → Synthesis → Stage5 → Check PASS → Audit PASS
   → **completed**（单次 run，1 stage4 + 1 stage5 run，无重复产物）；
2. Stage5 → **waiting_human** → approve action → **same Stage5 run resume** →
   orchestration completed（无重复 Stage5）；
3. waiting_human → rewrite action → **Stage5 revision**（新 Report + 新 Audit pass）
   → completed；
4. **research route**（audit research_required）→ ResearchBackflowRequest →
   orchestration phase=**research_backflow**、status=waiting_human、**no auto
   research**（无新 WorkflowRun / 无 fulfillment）；
5. **crash after Stage4 before Stage5** → 重启同顶层 thread → Stage5 执行 →
   completed、**no duplicate Stage4**；
6. **crash after Stage5 completed before top-level projection** → 重启恢复 →
   跳过重复 Stage5 → completed（Stage5 execute 只调 1 次）；
7. failed O1（Stage4 child failure）→ user retry → O2：**new id / new thread /
   attempt=2 / retry_of=O1 / same fingerprint / same Plan** → 跑 O2 → completed，
   O1 保持 failed 原样。
"""

import asyncio
import time
from types import SimpleNamespace
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.analysis.claims.contracts import ClaimAnalysisDecision, ClaimCandidate
from app.analysis.financial.errors import FinancialAnalysisModelUnavailable
from app.claims.contracts import ClaimConfidence, ClaimImportance, ClaimKind
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.research_orchestration.contracts import RESEARCH_BACKFLOW_LIMIT_REACHED
from app.research_orchestration.dependencies import ResearchOrchestrationDependencies
from app.research_orchestration.recovery import ResearchOrchestrationRecoveryCoordinator
from app.research_orchestration.runner import ResearchOrchestrationRunner
from app.research_orchestration.service import (
    ResearchOrchestrationChildService,
    ResearchOrchestrationService,
)
from app.research_planning.preparation import (
    MissingReasonCode,
    MissingResearchNeed,
    ResearchPreparationResult,
)
from app.research_planning.router import ResearchSourceRouter
from app.research_planning.service import ResearchPlanningService
from app.services.company_identity_service import CompanyIdentityService
from app.services.source_registry_service import SourceRegistryService
from app.services.workflow_recovery_service import WorkflowRecoveryService
from app.stage4.contracts import Stage4WorkflowRequest
from app.stage4.runner import Stage4WorkflowRunner
from app.stage5.runner import Stage5WorkflowRunner
from app.storage.raw_store import LocalRawArtifactStore
from app.synthesis.service import SynthesisService
from app.workflows.checkpoint import LangGraphCheckpointManager
from tests.analysis.financial.fakes import FakeFinancialAnalysisModel
from tests.audit.fakes import FakeAuditModel, pass_decision
from tests.draft_section.fakes import FakeDraftSectionModel, valid_decision_for
from tests.integration.test_claim_analysis_service import (
    _seed_document_card as _seed_claim_doc_card,
)
from tests.integration.test_report_audit_service import (
    human_review_decision,
    research_decision,
)
from tests.integration.test_research_planning_service import (
    _plan_payload,
    _seed_research_task,
)
from tests.integration.test_revision_service import _cleanup_with_revisions
from tests.integration.test_stage4_workflow import (
    _QUESTION,
    _claim_count_for_company,
    _good_models,
    _request,
    _seed_worker_inputs,
    _synthesis_counts,
)
from tests.integration.test_stage4_workflow import (
    _build_deps as _stage4_deps,
)
from tests.integration.test_stage5_workflow import _stage5_deps
from tests.integration.test_valuation_claim_service import _seed_company
from tests.research_planning.fakes import FakeResearchPlannerModel
from tests.revision.fakes import FakeRevisionWriterModel, revision_decision_for

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()


# ---------------------------------------------------------------- cleanup / env


async def _cleanup(sessionmaker) -> None:
    """先删 orchestration / plan 层（FK RESTRICT 引用 workflow_runs /
    research_plans / research_tasks），再走公共 `_cleanup_with_revisions`（Stage5
    reports / audits / revisions / human decisions / backflow requests）。"""
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM research_orchestration_child_runs"))
        await session.execute(text("DELETE FROM research_orchestration_runs"))
        await session.execute(text("DELETE FROM research_plan_routes"))
        await session.execute(text("DELETE FROM research_plans"))
        await session.commit()
    await _cleanup_with_revisions(sessionmaker)


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


# ---------------------------------------------------------------- fake services


class _FakePreparation:
    """可控 readiness 的 prepare：按调用次序返回结果（最后一个结果重复）。"""

    def __init__(self, outcomes) -> None:
        self._outcomes = outcomes
        self.calls = 0

    async def prepare_research(self, research_plan_id: UUID) -> ResearchPreparationResult:
        idx = min(self.calls, len(self._outcomes) - 1)
        self.calls += 1
        ready, request, missing_codes = self._outcomes[idx]
        return ResearchPreparationResult(
            research_plan_id=research_plan_id,
            resolved=(),
            module_inputs=(),
            missing_needs=tuple(
                MissingResearchNeed(code, "document", MissingReasonCode.NOT_FOUND, "fake missing")
                for code in missing_codes
            ),
            ready_for_analysis=ready,
            stage4_request=request,
        )


class _FakeFulfillment:
    """记录调用的 fulfill（readiness 由 fake preparation 控制）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def fulfill_research_needs(self, research_plan_id: UUID):
        self.calls += 1


class _SequencedAuditDecision:
    """按调用次序返回不同 audit decision（最后重复）；rewrite 后 pass 用。"""

    def __init__(self, *factories) -> None:
        self._factories = list(factories)
        self.calls = 0

    def __call__(self, pack):
        idx = min(self.calls, len(self._factories) - 1)
        self.calls += 1
        return self._factories[idx](pack)


class _FakeBackflowExecutor:
    """可控 `ResearchBackflowExecutor`：按调用次序返回预置 new_evidence_card_ids
    （最后重复）。只投影 `new_evidence_card_ids`（graph 节点只读它；真实检索链
    已由 test_research_backflow_executor.py 覆盖，0 real DeepSeek）。"""

    def __init__(self, new_card_batches=()) -> None:
        self._batches = list(new_card_batches)
        self.calls = 0
        self.executed: list = []

    async def execute_supplemental_research(self, verified_request, plan_payload):
        self.calls += 1
        self.executed.append((verified_request, plan_payload))
        idx = min(self.calls - 1, len(self._batches) - 1) if self._batches else -1
        return SimpleNamespace(
            new_evidence_card_ids=tuple(self._batches[idx]) if self._batches else ()
        )


def _with_extra_card(request: Stage4WorkflowRequest, extra_card_id) -> Stage4WorkflowRequest:
    """把新卡追加到 business work item（模拟真实 prepare 把新 EvidenceCard 经
    research_question_sha256 过滤纳入 stage4_request）。extra_card_id 可能是
    str / UUID（seed helper 返回 UUID）；work item 保持 Pydantic model（Stage4
    `_build_initial_state` 对 item 调 model_dump）。"""
    extra_uuid = UUID(str(extra_card_id))
    items = [
        item.model_copy(update={"evidence_card_ids": item.evidence_card_ids + [extra_uuid]})
        if item.item_id == "biz"
        else item
        for item in request.analysis_work_items
    ]
    return request.model_copy(update={"analysis_work_items": items})


class _RefAwareClaimModel:
    """Deterministic claim model：decision 引用 evidence pack 全部 refs。

    fake claim 分析按 str(card_id) 排序分配 E1..En；若只引 E1（如
    `_claim_decision`），新增卡除非恰好成为最小 UUID 卡，否则不改变 claim 的
    evidence relations → fingerprint 不变 → backflow fulfill 误判 no progress。
    引用**全部** refs 保证：work item 纳入新 EvidenceCard → 新 support ref →
    新 claim fingerprint → 新 synthesis run（确定性，不依赖 UUID 排序）。
    """

    model_id = "ref-aware-claim"

    async def analyze(self, context, evidence_pack):
        self.calls = getattr(self, "calls", [])
        self.calls.append((context, evidence_pack))
        return ClaimAnalysisDecision(
            relevant=True,
            claims=[
                ClaimCandidate(
                    statement="ref-aware claim",
                    claim_kind=ClaimKind.INFERENCE,
                    confidence=ClaimConfidence.MEDIUM,
                    importance=ClaimImportance.NORMAL,
                    support_refs=[item.evidence_ref for item in evidence_pack.items],
                    contradict_refs=[],
                    context_refs=[],
                )
            ],
        )


def _ref_aware_models() -> dict:
    """`_good_models()` + ref-aware claim model（biz/rsk 用）。"""
    return {**_good_models(), "claim": _RefAwareClaimModel()}


def _planner(sessionmaker) -> ResearchPlanningService:
    return ResearchPlanningService(
        sessionmaker,
        FakeResearchPlannerModel(payload=_plan_payload()),
        CompanyIdentityService(sessionmaker),
    )


def _audit_model(decision_factory) -> FakeAuditModel:
    return FakeAuditModel(decision_factory=decision_factory)


def _stage5_deps_for(sessionmaker, audit_model, *, revision_model=None):
    return _stage5_deps(
        sessionmaker,
        draft_model=FakeDraftSectionModel(decision_factory=valid_decision_for),
        audit_model=audit_model,
        revision_model=revision_model or FakeRevisionWriterModel(),
    )


def _orchestration_deps(
    sessionmaker,
    manager,
    request,
    *,
    audit_model,
    prep_outcomes=None,
    models=None,
    revision_model=None,
    backflow_executor=None,
) -> ResearchOrchestrationDependencies:
    """完整顶层 deps：plan/router/stage4/synthesis/stage5，fake prepare/fulfill/models。

    `backflow_executor`（7A.2B.3）：注入后 research_required 触发 backflow loop；
    真实 `research_backflow_service` 始终绑定（stage5 deps 内构造，0 model call）。
    """
    prep_outcomes = prep_outcomes or [(True, request, [])]
    plan_service = _planner(sessionmaker)
    router = ResearchSourceRouter(sessionmaker, plan_service)
    stage4_deps = _stage4_deps(sessionmaker, models if models is not None else _good_models())
    stage4_runner = Stage4WorkflowRunner(sessionmaker, manager, stage4_deps)
    stage5_runner = Stage5WorkflowRunner(
        sessionmaker,
        manager,
        _stage5_deps_for(sessionmaker, audit_model, revision_model=revision_model),
    )
    child_service = ResearchOrchestrationChildService(
        sessionmaker, stage4_runner, stage5_runner=stage5_runner
    )
    return ResearchOrchestrationDependencies(
        sessionmaker=sessionmaker,
        plan_service=plan_service,
        router=router,
        preparation=_FakePreparation(prep_outcomes),
        fulfillment=_FakeFulfillment(),
        child_service=child_service,
        stage4_runner=stage4_runner,
        synthesis_service=SynthesisService(sessionmaker),
        stage5_runner=stage5_runner,
        backflow_service=stage5_runner.dependencies.research_backflow_service,
        backflow_executor=backflow_executor,
    )


def _bound_service(sessionmaker, deps, runner) -> ResearchOrchestrationService:
    """绑定 stage5_runner + orchestration_runner 的 service（human action 需要）。"""
    return ResearchOrchestrationService(
        sessionmaker,
        deps.plan_service,
        stage5_runner=deps.stage5_runner,
        orchestration_runner=runner,
    )


async def _create_orchestration(sessionmaker, task_id: UUID) -> UUID:
    plan_service = _planner(sessionmaker)
    service = ResearchOrchestrationService(sessionmaker, plan_service)
    result = await service.create_or_get_orchestration(task_id)
    assert result.replayed is False
    return result.orchestration_id


# ---------------------------------------------------------------- read helpers


async def _get_orchestration_row(sessionmaker, orchestration_id: UUID) -> dict:
    async with sessionmaker() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT status, current_phase, error_code, research_plan_id, "
                        "input_fingerprint, attempt_no, retry_of_orchestration_id "
                        "FROM research_orchestration_runs WHERE orchestration_id = :oid"
                    ).bindparams(oid=orchestration_id)
                )
            )
            .mappings()
            .one()
        )
        return dict(row)


async def _get_child(
    sessionmaker, orchestration_id: UUID, stage: str, *, attempt_no: int = 1
) -> dict | None:
    async with sessionmaker() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT workflow_run_id::text AS run_id, stage, attempt_no, "
                        "source_research_request_id::text AS source_research_request_id "
                        "FROM research_orchestration_child_runs "
                        "WHERE orchestration_id = :oid AND stage = :stage AND attempt_no = :an"
                    ).bindparams(oid=orchestration_id, stage=stage, an=attempt_no)
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None


async def _get_child_attempt(
    sessionmaker, orchestration_id: UUID, stage: str, attempt_no: int
) -> dict | None:
    """按 attempt_no 精确取 child（backflow 断言用）。"""
    return await _get_child(sessionmaker, orchestration_id, stage, attempt_no=attempt_no)


async def _runs_for_task(sessionmaker, task_id: UUID) -> list[dict]:
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT run_id::text AS run_id, graph_name, status, error_code "
                    "FROM workflow_runs WHERE task_id = :tid ORDER BY created_at, run_id"
                ).bindparams(tid=task_id)
            )
        ).mappings()
        return [dict(r) for r in rows]


async def _run_status(sessionmaker, run_id: UUID) -> str:
    async with sessionmaker() as session:
        return (
            await session.execute(
                text("SELECT status FROM workflow_runs WHERE run_id = :rid").bindparams(rid=run_id)
            )
        ).scalar_one()


async def _count(sessionmaker, table: str) -> int:
    async with sessionmaker() as session:
        return int((await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())


async def _wait_until(predicate, *, timeout: float = 60.0, message: str) -> None:
    deadline = time.monotonic() + timeout
    while True:
        if await predicate():
            return
        if time.monotonic() > deadline:
            raise AssertionError(message)
        await asyncio.sleep(0.2)


# ---------------------------------------------------------------- Case 1


async def test_case1_full_chain_to_completed(env, monkeypatch, connection_uri) -> None:
    """Case 1：Task → orchestration → Stage4 → Synthesis → Stage5 → Check PASS →
    Audit PASS → completed（单次 run，无重复产物）。"""
    ids = await _seed_worker_inputs(env, monkeypatch)
    request = _request(env, ids)
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]
    company_id = env["company_id"]

    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        orchestration_id = await _create_orchestration(sessionmaker, task_id)
        deps = _orchestration_deps(
            sessionmaker, manager, request, audit_model=_audit_model(pass_decision)
        )
        runner = ResearchOrchestrationRunner(sessionmaker, manager, deps)
        final = await runner.run_orchestration(orchestration_id)

        assert final["current_phase"] == "completed"
        row = await _get_orchestration_row(sessionmaker, orchestration_id)
        assert row["status"] == "completed"
        assert row["current_phase"] == "completed"
        assert row["error_code"] is None

        # 恰好 1 stage4 + 1 stage5 run，全部 completed。
        runs = await _runs_for_task(sessionmaker, task_id)
        assert len(runs) == 2
        assert {r["graph_name"] for r in runs} == {"stage4_analysis", "stage5_report"}
        assert all(r["status"] == "completed" for r in runs)
        child4 = await _get_child(sessionmaker, orchestration_id, "stage4")
        child5 = await _get_child(sessionmaker, orchestration_id, "stage5")
        assert child4 is not None and child5 is not None
        assert {c["run_id"] for c in (child4, child5)} == {r["run_id"] for r in runs}

        # 真实产物：5 claims + 1 synthesis + 1 report，0 人工决策。
        assert await _claim_count_for_company(sessionmaker, company_id) == 5
        s_runs, s_results = await _synthesis_counts(sessionmaker)
        assert (s_runs, s_results) == (1, 1)
        assert await _count(sessionmaker, "reports") == 1
        assert await _count(sessionmaker, "human_review_decisions") == 0
        assert deps.fulfillment.calls == 0
    finally:
        await manager.close()


# ---------------------------------------------------------------- Case 2


async def test_case2_waiting_human_approve_same_run(env, monkeypatch, connection_uri) -> None:
    """Case 2：Stage5 → waiting_human → approve action → same Stage5 run resume →
    orchestration completed（无重复 Stage5）。"""
    ids = await _seed_worker_inputs(env, monkeypatch)
    request = _request(env, ids)
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]

    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        orchestration_id = await _create_orchestration(sessionmaker, task_id)
        deps = _orchestration_deps(
            sessionmaker, manager, request, audit_model=_audit_model(human_review_decision)
        )
        runner = ResearchOrchestrationRunner(sessionmaker, manager, deps)
        final = await runner.run_orchestration(orchestration_id)

        assert final["current_phase"] == "awaiting_stage5"
        row = await _get_orchestration_row(sessionmaker, orchestration_id)
        assert row["status"] == "waiting_human"
        assert row["current_phase"] == "awaiting_stage5"
        child5 = await _get_child(sessionmaker, orchestration_id, "stage5")
        assert child5 is not None
        stage5_run_id = child5["run_id"]
        assert await _run_status(sessionmaker, UUID(stage5_run_id)) == "waiting_human"

        # approve → same Stage5 run resume → orchestration complete。
        service = _bound_service(sessionmaker, deps, runner)
        result = await service.act_on_orchestration(orchestration_id, "approve", "审核通过")
        assert result.status == "completed"
        row = await _get_orchestration_row(sessionmaker, orchestration_id)
        assert row["status"] == "completed"
        assert row["current_phase"] == "completed"

        # 无重复 Stage5：仍 1 个 stage5 run（same run_id）、completed。
        runs = await _runs_for_task(sessionmaker, task_id)
        stage5_runs = [r for r in runs if r["graph_name"] == "stage5_report"]
        assert len(stage5_runs) == 1
        assert stage5_runs[0]["run_id"] == stage5_run_id
        assert stage5_runs[0]["status"] == "completed"
        assert len(runs) == 2
        assert await _count(sessionmaker, "reports") == 1
        assert await _count(sessionmaker, "human_review_decisions") == 1
    finally:
        await manager.close()


# ---------------------------------------------------------------- Case 3


async def test_case3_rewrite_revision_complete(env, monkeypatch, connection_uri) -> None:
    """Case 3：waiting_human → rewrite action → Stage5 revision（新 Report + 新 Audit
    pass）→ completed。"""
    ids = await _seed_worker_inputs(env, monkeypatch)
    request = _request(env, ids)
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]

    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        orchestration_id = await _create_orchestration(sessionmaker, task_id)
        # 第 1 次 audit → human_review（interrupt）；rewrite 后第 2 次 audit → pass。
        audit = _SequencedAuditDecision(human_review_decision, pass_decision)
        deps = _orchestration_deps(
            sessionmaker,
            manager,
            request,
            audit_model=_audit_model(audit),
            revision_model=FakeRevisionWriterModel(decision_factory=revision_decision_for),
        )
        runner = ResearchOrchestrationRunner(sessionmaker, manager, deps)
        final = await runner.run_orchestration(orchestration_id)
        assert final["current_phase"] == "awaiting_stage5"
        assert await _count(sessionmaker, "reports") == 1

        # rewrite → Stage5 revision（round 2）→ 新 Audit pass → finalize → complete。
        service = _bound_service(sessionmaker, deps, runner)
        result = await service.act_on_orchestration(orchestration_id, "rewrite", "请重新表述")
        assert result.status == "completed"
        row = await _get_orchestration_row(sessionmaker, orchestration_id)
        assert row["status"] == "completed"
        assert row["current_phase"] == "completed"

        # Stage5 revision 发生：1 条 revision + 2 份 Report（初始 + 修订）+ 1 次 human review。
        assert await _count(sessionmaker, "draft_section_revisions") == 1
        assert await _count(sessionmaker, "reports") == 2
        assert await _count(sessionmaker, "human_review_decisions") == 1
        runs = await _runs_for_task(sessionmaker, task_id)
        assert len(runs) == 2
        assert all(r["status"] == "completed" for r in runs)
    finally:
        await manager.close()


# ---------------------------------------------------------------- Case 4


async def test_case4_research_route_no_progress_manual(env, monkeypatch, connection_uri) -> None:
    """Case 4（7A.2B.3）：research_required → backflow loop round1 → 无新增
    EvidenceCard → verify no_progress → research_backflow_manual
    （reason=research_backflow_no_progress）→ waiting_human；真实补充计划落库、
    无新 child / 无 fulfillment（不假装完成）。"""
    ids = await _seed_worker_inputs(env, monkeypatch)
    request = _request(env, ids)
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]

    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        orchestration_id = await _create_orchestration(sessionmaker, task_id)
        executor = _FakeBackflowExecutor()  # 0 卡 → no_progress
        deps = _orchestration_deps(
            sessionmaker,
            manager,
            request,
            audit_model=_audit_model(research_decision),
            backflow_executor=executor,
        )
        runner = ResearchOrchestrationRunner(sessionmaker, manager, deps)
        final = await runner.run_orchestration(orchestration_id)

        assert final["current_phase"] == "research_backflow"
        assert final.get("research_request_id")
        assert final["backflow_round"] == 1
        assert final["backflow_manual_reason"] == "research_backflow_no_progress"
        assert final.get("backflow_plan_id")
        row = await _get_orchestration_row(sessionmaker, orchestration_id)
        assert row["status"] == "waiting_human"
        assert row["current_phase"] == "research_backflow"
        # ResearchBackflowRequest 已持久化（可验证交接请求，spec P）；补充计划 1 行。
        assert await _count(sessionmaker, "research_backflow_requests") == 1
        assert await _count(sessionmaker, "research_backflow_plans") == 1
        assert await _count(sessionmaker, "research_backflow_fulfillments") == 0

        # no auto research：无新 WorkflowRun（仍 1 stage4 + 1 stage5）/ fulfillment 未触发。
        runs = await _runs_for_task(sessionmaker, task_id)
        assert len(runs) == 2
        assert all(r["status"] == "completed" for r in runs)
        assert deps.fulfillment.calls == 0
        assert executor.calls == 1
        # 无 Stage4/Stage5 backflow attempt（child 只有 attempt 1）。
        assert await _get_child_attempt(sessionmaker, orchestration_id, "stage4", 2) is None
        assert await _get_child_attempt(sessionmaker, orchestration_id, "stage5", 2) is None
        # 顶层 checkpoint 携带 research_request_id（backflow loop input 唯一入口）。
        checkpoint = await runner.read_orchestration_checkpoint(orchestration_id)
        assert checkpoint.get("research_request_id")
        assert checkpoint.get("backflow_plan_id")
    finally:
        await manager.close()


# ---------------------------------------------------------------- Case 5


async def test_case5_crash_after_stage4_before_stage5(env, monkeypatch, connection_uri) -> None:
    """Case 5：crash after Stage4 before Stage5 → 重启同顶层 thread → Stage5 执行 →
    completed、no duplicate Stage4。"""
    ids = await _seed_worker_inputs(env, monkeypatch)
    request = _request(env, ids)
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]
    company_id = env["company_id"]

    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        orchestration_id = await _create_orchestration(sessionmaker, task_id)
        deps = _orchestration_deps(
            sessionmaker, manager, request, audit_model=_audit_model(pass_decision)
        )
        runner = ResearchOrchestrationRunner(sessionmaker, manager, deps)

        # 进程 A：Stage5 execute 前 gate → 顶层阻塞（crash window：Stage4 完成、
        # Stage5 child 创建但未执行）。
        gate = asyncio.Event()
        orig_execute = deps.stage5_runner.execute_stage5

        async def gated_execute(run_id, req):
            await gate.wait()
            return await orig_execute(run_id, req)

        monkeypatch.setattr(deps.stage5_runner, "execute_stage5", gated_execute)
        task = asyncio.create_task(runner.run_orchestration(orchestration_id))

        async def _stage5_child_created() -> bool:
            return await _get_child(sessionmaker, orchestration_id, "stage5") is not None

        await _wait_until(
            _stage5_child_created,
            message="Stage5 child 未在超时前创建",
        )
        runs = await _runs_for_task(sessionmaker, task_id)
        assert len(runs) == 2
        assert [r["graph_name"] for r in runs] == ["stage4_analysis", "stage5_report"]
        assert runs[0]["status"] == "completed"
        assert runs[1]["status"] == "pending"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # 重启路径：真实 reconcile → pending Stage5 child → FAILED(worker_restarted)。
        recovery = WorkflowRecoveryService(sessionmaker)
        assert (await recovery.reconcile_orphaned_runs()).marked_failed == 1

        # 进程 B：coordinator 恢复同顶层 thread → Stage5 resume → completed。
        deps_b = _orchestration_deps(
            sessionmaker, manager, request, audit_model=_audit_model(pass_decision)
        )
        runner_b = ResearchOrchestrationRunner(sessionmaker, manager, deps_b)
        coordinator = ResearchOrchestrationRecoveryCoordinator(sessionmaker, runner_b)
        assert await coordinator.recover_orchestrations() == 1

        row = await _get_orchestration_row(sessionmaker, orchestration_id)
        assert row["status"] == "completed"
        assert row["current_phase"] == "completed"
        # no duplicate Stage4：仍 1 stage4 + 1 stage5，全部 completed。
        runs = await _runs_for_task(sessionmaker, task_id)
        assert len(runs) == 2
        assert all(r["status"] == "completed" for r in runs)
        assert await _claim_count_for_company(sessionmaker, company_id) == 5
        s_runs, s_results = await _synthesis_counts(sessionmaker)
        assert (s_runs, s_results) == (1, 1)
    finally:
        await manager.close()


# ---------------------------------------------------------------- Case 6


async def test_case6_crash_after_stage5_before_projection(env, monkeypatch, connection_uri) -> None:
    """Case 6：crash after Stage5 completed before top-level projection → 重启恢复 →
    跳过重复 Stage5 → completed（Stage5 execute 只调 1 次）。"""
    ids = await _seed_worker_inputs(env, monkeypatch)
    request = _request(env, ids)
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]

    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        orchestration_id = await _create_orchestration(sessionmaker, task_id)
        deps = _orchestration_deps(
            sessionmaker, manager, request, audit_model=_audit_model(pass_decision)
        )
        runner = ResearchOrchestrationRunner(sessionmaker, manager, deps)

        # 进程 A：Stage5 execute 完成后 gate → Stage5 run completed 但顶层未投影
        # complete（crash window：Stage5 done、top-level projection not yet）。
        gate = asyncio.Event()
        orig_execute = deps.stage5_runner.execute_stage5

        async def gated_execute(run_id, req):
            result = await orig_execute(run_id, req)
            await gate.wait()
            return result

        monkeypatch.setattr(deps.stage5_runner, "execute_stage5", gated_execute)
        task = asyncio.create_task(runner.run_orchestration(orchestration_id))

        async def _stage5_completed() -> bool:
            child = await _get_child(sessionmaker, orchestration_id, "stage5")
            if child is None:
                return False
            return await _run_status(sessionmaker, UUID(child["run_id"])) == "completed"

        await _wait_until(
            _stage5_completed,
            message="Stage5 run 未在超时前 completed",
        )
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # 重启路径：coordinator 恢复 → run_or_resume_stage5 重查 child（completed →
        # 跳过 execute）→ complete_orchestration → completed。
        deps_b = _orchestration_deps(
            sessionmaker, manager, request, audit_model=_audit_model(pass_decision)
        )
        runner_b = ResearchOrchestrationRunner(sessionmaker, manager, deps_b)
        calls = {"execute": 0}
        orig_b = deps_b.stage5_runner.execute_stage5

        async def counted_execute(run_id, req):
            calls["execute"] += 1
            return await orig_b(run_id, req)

        monkeypatch.setattr(deps_b.stage5_runner, "execute_stage5", counted_execute)
        coordinator = ResearchOrchestrationRecoveryCoordinator(sessionmaker, runner_b)
        assert await coordinator.recover_orchestrations() == 1

        row = await _get_orchestration_row(sessionmaker, orchestration_id)
        assert row["status"] == "completed"
        assert row["current_phase"] == "completed"
        # no duplicate Stage5：execute 未再调用、仍 1 份 Report / 2 runs。
        assert calls["execute"] == 0
        assert await _count(sessionmaker, "reports") == 1
        runs = await _runs_for_task(sessionmaker, task_id)
        assert len(runs) == 2
        assert all(r["status"] == "completed" for r in runs)
    finally:
        await manager.close()


# ---------------------------------------------------------------- Case 7


class _SlowFailingFinancial(FakeFinancialAnalysisModel):
    """慢失败：让其余 worker 完成并 checkpoint，再抛 provider 错误（O1 失败于
    stage4 child 执行）。"""

    async def analyze(self, context, calculation_pack, evidence_pack):
        self.calls.append((context, calculation_pack, evidence_pack))
        await asyncio.sleep(0.5)
        raise FinancialAnalysisModelUnavailable()


async def test_case7_failed_retry_new_id_thread_attempt2(env, monkeypatch, connection_uri) -> None:
    """Case 7：failed O1 → user retry O2（new id / new thread / attempt=2 /
    retry_of=O1 / same fingerprint / same Plan）→ 跑 O2 → completed；O1 保持 failed。"""
    ids = await _seed_worker_inputs(env, monkeypatch)
    request = _request(env, ids)
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]

    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        # O1 失败（stage4 child failure）。
        o1 = await _create_orchestration(sessionmaker, task_id)
        models_bad = _good_models()
        models_bad["financial"] = _SlowFailingFinancial()
        deps_bad = _orchestration_deps(
            sessionmaker,
            manager,
            request,
            audit_model=_audit_model(pass_decision),
            models=models_bad,
        )
        runner_bad = ResearchOrchestrationRunner(sessionmaker, manager, deps_bad)
        with pytest.raises(FinancialAnalysisModelUnavailable):
            await runner_bad.run_orchestration(o1)
        row1 = await _get_orchestration_row(sessionmaker, o1)
        assert row1["status"] == "failed"
        assert row1["current_phase"] == "stage4"
        assert row1["error_code"] == "stage4_execution_failed"
        fp1 = row1["input_fingerprint"]
        plan1 = row1["research_plan_id"]

        # user retry → O2（same Plan / same fingerprint / new id / attempt=2）。
        service = ResearchOrchestrationService(sessionmaker, deps_bad.plan_service)
        o2 = await service.retry_orchestration(o1)
        assert o2.orchestration_id != o1
        assert o2.attempt_no == 2
        assert o2.retry_of_orchestration_id == o1
        assert o2.research_plan_id == plan1
        assert o2.input_fingerprint == fp1
        assert o2.status == "pending"
        assert o2.current_phase == "planning"
        assert await _count(sessionmaker, "research_orchestration_runs") == 2

        # O2 新顶层 thread 跑（good models）→ completed；O1 原样保留。
        deps_good = _orchestration_deps(
            sessionmaker, manager, request, audit_model=_audit_model(pass_decision)
        )
        runner_good = ResearchOrchestrationRunner(sessionmaker, manager, deps_good)
        await runner_good.run_orchestration(o2.orchestration_id)
        row2 = await _get_orchestration_row(sessionmaker, o2.orchestration_id)
        assert row2["status"] == "completed"
        assert row2["current_phase"] == "completed"
        assert row2["research_plan_id"] == plan1
        assert row2["input_fingerprint"] == fp1
        assert row2["attempt_no"] == 2

        row1b = await _get_orchestration_row(sessionmaker, o1)
        assert row1b["status"] == "failed"
        assert row1b["current_phase"] == "stage4"
        assert row1b["attempt_no"] == 1
        assert row1b["retry_of_orchestration_id"] is None
    finally:
        await manager.close()


# ---------------------------------------------------------------- backflow（7A.2B.3）


async def test_backflow_progress_fulfill_continuation_completed(
    env, monkeypatch, connection_uri
) -> None:
    """Backflow happy path：research_required → loop round1 → 真实补充计划 + 新增
    EvidenceCard（fake executor 返回 seed 的真实卡）→ Stage4 attempt2（新卡纳入 →
    新 Synthesis fingerprint）→ 真实 fulfill_request 落库 → Stage5 attempt2
    continuation（source_research_request_id 记录）→ audit pass → completed。"""
    ids = await _seed_worker_inputs(env, monkeypatch)
    extra = await _seed_claim_doc_card(
        env,
        statement="贵州茅台 2026 年新增经销商渠道证据。",
        source_url="https://www.xinhuanet.com/2026/0810/s4extra.htm",
        research_question=_QUESTION,
    )
    request = _request(env, ids)
    request2 = _with_extra_card(request, extra["evidence_card_id"])
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]

    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        orchestration_id = await _create_orchestration(sessionmaker, task_id)
        audit = _SequencedAuditDecision(research_decision, pass_decision)
        executor = _FakeBackflowExecutor(new_card_batches=[(extra["evidence_card_id"],)])
        # prepare 调用序：首启 3 次（prepare + ensure_stage4_child +
        # run_or_resume_stage4 都用 base）→ backflow 2 次（prepare_updated_analysis
        # + run_or_resume_stage4 用 request2）。
        deps = _orchestration_deps(
            sessionmaker,
            manager,
            request,
            audit_model=_audit_model(audit),
            prep_outcomes=[
                (True, request, []),
                (True, request, []),
                (True, request, []),
                (True, request2, []),
            ],
            models=_ref_aware_models(),
            backflow_executor=executor,
        )
        runner = ResearchOrchestrationRunner(sessionmaker, manager, deps)
        final = await runner.run_orchestration(orchestration_id)

        # completed，backflow round1 正常消费。
        assert final["current_phase"] == "completed"
        assert final["backflow_round"] == 1
        assert final.get("fulfillment_id")
        row = await _get_orchestration_row(sessionmaker, orchestration_id)
        assert row["status"] == "completed"

        # 真实补充计划 + fulfillment 各 1 行；Stage4 attempt2 确实重分析。
        assert await _count(sessionmaker, "research_backflow_plans") == 1
        assert await _count(sessionmaker, "research_backflow_fulfillments") == 1
        assert executor.calls == 1
        s_runs, s_results = await _synthesis_counts(sessionmaker)
        assert (s_runs, s_results) == (2, 2)

        # child：Stage4/Stage5 attempt1 + attempt2；attempt2 记录 source_research_request_id。
        child4_1 = await _get_child(sessionmaker, orchestration_id, "stage4", attempt_no=1)
        child4_2 = await _get_child(sessionmaker, orchestration_id, "stage4", attempt_no=2)
        child5_1 = await _get_child(sessionmaker, orchestration_id, "stage5", attempt_no=1)
        child5_2 = await _get_child(sessionmaker, orchestration_id, "stage5", attempt_no=2)
        assert child4_1 is not None and child5_1 is not None
        assert child4_2 is not None and child5_2 is not None
        assert child4_2["source_research_request_id"] is not None
        assert child5_2["source_research_request_id"] is not None
        assert child4_1["source_research_request_id"] is None
        assert child5_1["source_research_request_id"] is None
        assert child4_1["run_id"] != child4_2["run_id"]
        assert child5_1["run_id"] != child5_2["run_id"]

        # 4 条 run（stage4 x2 + stage5 x2）全部 completed。
        runs = await _runs_for_task(sessionmaker, task_id)
        assert len(runs) == 4
        assert all(r["status"] == "completed" for r in runs)
    finally:
        await manager.close()


async def test_backflow_loop_reaches_limit_manual(env, monkeypatch, connection_uri) -> None:
    """Backflow 达上限：两轮各新增 EvidenceCard + 两轮 fulfill 成功 → Stage5 第三次
    research_required → round=2 >= MAX → research_backflow_manual
    （reason=research_backflow_limit_reached）→ waiting_human；3 次 Stage4/Stage5
    attempt、2 行 fulfillment。"""
    ids = await _seed_worker_inputs(env, monkeypatch)
    extra_a = await _seed_claim_doc_card(
        env,
        statement="贵州茅台 2026 年渠道证据 A。",
        source_url="https://www.xinhuanet.com/2026/0810/s4extraA.htm",
        research_question=_QUESTION,
    )
    extra_b = await _seed_claim_doc_card(
        env,
        statement="贵州茅台 2026 年渠道证据 B。",
        source_url="https://www.xinhuanet.com/2026/0810/s4extraB.htm",
        research_question=_QUESTION,
    )
    request = _request(env, ids)
    request_a = _with_extra_card(request, extra_a["evidence_card_id"])
    request_ab = _with_extra_card(request_a, extra_b["evidence_card_id"])
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]

    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        orchestration_id = await _create_orchestration(sessionmaker, task_id)
        executor = _FakeBackflowExecutor(
            new_card_batches=[
                (extra_a["evidence_card_id"],),
                (extra_b["evidence_card_id"],),
            ]
        )
        # prepare 调用序：首启 3 次 base → round1 2 次 request_a → round2 2 次
        # request_ab（prepare/ensure_stage4_child/run_or_resume_stage4 +
        # prepare_updated_analysis/run_or_resume_stage4）。
        deps = _orchestration_deps(
            sessionmaker,
            manager,
            request,
            audit_model=_audit_model(research_decision),  # 每轮都 research_required
            prep_outcomes=[
                (True, request, []),
                (True, request, []),
                (True, request, []),
                (True, request_a, []),
                (True, request_a, []),
                (True, request_ab, []),
            ],
            models=_ref_aware_models(),
            backflow_executor=executor,
        )
        runner = ResearchOrchestrationRunner(sessionmaker, manager, deps)
        final = await runner.run_orchestration(orchestration_id)

        # 达上限 → research_backflow_manual（稳定 reason），不 pretend completed。
        assert final["current_phase"] == "research_backflow"
        assert final["backflow_round"] == 2
        assert final["backflow_manual_reason"] == RESEARCH_BACKFLOW_LIMIT_REACHED
        row = await _get_orchestration_row(sessionmaker, orchestration_id)
        assert row["status"] == "waiting_human"
        assert row["current_phase"] == "research_backflow"

        # 两轮补充计划 + 两轮 fulfillment；Stage4/Stage5 共 3 次 attempt。
        assert await _count(sessionmaker, "research_backflow_plans") == 2
        assert await _count(sessionmaker, "research_backflow_fulfillments") == 2
        assert executor.calls == 2
        s_runs, s_results = await _synthesis_counts(sessionmaker)
        assert (s_runs, s_results) == (3, 3)
        assert (
            await _get_child(sessionmaker, orchestration_id, "stage4", attempt_no=3) is not None
        )
        assert (
            await _get_child(sessionmaker, orchestration_id, "stage5", attempt_no=3) is not None
        )

        # 6 条 run（stage4 x3 + stage5 x3）全部 completed（child 终态），顶层 waiting_human。
        runs = await _runs_for_task(sessionmaker, task_id)
        assert len(runs) == 6
        assert all(r["status"] == "completed" for r in runs)
    finally:
        await manager.close()
