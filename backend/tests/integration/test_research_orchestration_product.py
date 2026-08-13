"""7A Product Gate 产品集成测试（spec P，Case 1-5）。

真实 PostgreSQL + 真实 LangGraph（PG Checkpointer）+ 真实 `research_orchestration_`
* 表 + 真实 Stage4/Stage5 runner + 真实 Synthesis；plan/router 真实
（FakeResearchPlannerModel），prepare/fulfill 注入可控 Fake，Stage4/Stage5 全部
Fake models。**全程零真实 DeepSeek / 零 live provider**。

与 `test_research_orchestration_stage5.py`（直接调 runner）的区别：本文件走**产品
闭环**——`ResearchOrchestrationService` 同时绑定 `orchestration_runner` +
`execution_manager`（`prepare_orchestration_start` 一键入口 → 后台调度真实顶层
图 → 轮询完成），人工补资料 / 人工裁决都经 service 的产品入口（
`resume_after_source_acquisition` / `act_on_orchestration`）。

Concentrated Product Cases（spec P）：
1. 一键入口（POST /tasks/{id}/orchestrations 语义）→ 后台真实顶层图 → Stage4 →
   Synthesis → Stage5 → audit pass → **completed**（单次 run，无重复产物）；
   完成后再次一键入口 → 返回**同一 orchestration**（不新建）。
2. **K1**：首启 not_ready → fulfill 后仍 not_ready → waiting_manual（0 child）；
   用户补 source（等价 source-records upload 成功）→
   `resume_after_source_acquisition` → **同 orchestration + 同顶层线程** →
   prepare 重算 ready → Stage4/Stage5 → completed（无重复 orchestration）。
3. **K2**：research_backflow + reason=source_acquisition_required → 补资料 →
   `resume_after_source_acquisition` → **同 research_request_id + 同 backflow_round**
   （不 round+1、replay 同 plan）→ execute_supplemental_research 产出新卡 →
   Stage4/Stage5 attempt2 → fulfill → audit pass → completed。
4. awaiting_stage5 → `act_on_orchestration(approve)`（产品入口）→ 同一 Stage5 run
   resume → completed（无重复 Stage5 / Report）。
5. **K3**：research_backflow_limit_reached（MAX rounds=2 不可绕过）→
   `resume_after_source_acquisition` → `ResearchOrchestrationInvalidAction`，
   orchestration 原样保留。
"""

from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.research_backflow.contracts import (
    RESEARCH_BACKFLOW_MANUAL_REASON_SOURCE_ACQUISITION,
    RESEARCH_BACKFLOW_NEED_STATUS_MANUAL_REQUIRED,
    ResearchBackflowExecutionResult,
    ResearchBackflowNeedExecution,
)
from app.research_orchestration.contracts import RESEARCH_BACKFLOW_LIMIT_REACHED
from app.research_orchestration.errors import ResearchOrchestrationInvalidAction
from app.research_orchestration.execution_manager import ResearchOrchestrationExecutionManager
from app.research_orchestration.runner import ResearchOrchestrationRunner
from app.research_orchestration.service import ResearchOrchestrationService
from app.research_planning.preparation import (
    MissingReasonCode,
    MissingResearchNeed,
    ResearchPreparationResult,
)
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from app.workflows.checkpoint import LangGraphCheckpointManager
from tests.audit.fakes import pass_decision
from tests.integration.test_claim_analysis_service import (
    _seed_document_card as _seed_claim_doc_card,
)
from tests.integration.test_report_audit_service import (
    human_review_decision,
    research_decision,
)
from tests.integration.test_research_orchestration_stage5 import (
    _audit_model,
    _count,
    _FakeBackflowExecutor,
    _get_child,
    _get_orchestration_row,
    _orchestration_deps,
    _ref_aware_models,
    _run_status,
    _runs_for_task,
    _SequencedAuditDecision,
    _wait_until,
    _with_extra_card,
)
from tests.integration.test_research_planning_service import _seed_research_task
from tests.integration.test_revision_service import _cleanup_with_revisions
from tests.integration.test_stage4_workflow import (
    _QUESTION,
    _claim_count_for_company,
    _request,
    _seed_worker_inputs,
    _synthesis_counts,
)
from tests.integration.test_valuation_claim_service import _seed_company

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()


# ---------------------------------------------------------------- cleanup / env


async def _cleanup(sessionmaker) -> None:
    """先删 orchestration / plan 层（FK RESTRICT），再走公共 Stage5 清理。"""
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


# ---------------------------------------------------------------- 产品闭环 harness


class _ProductHarness:
    """产品闭环：一键入口（prepare_orchestration_start）+ 后台执行（manager）+
    人工补资料 / 人工裁决的完整 bound service。

    service / runner / execution_manager 共享同一 runner 实例（production
    factory 语义），背景任务在事件循环内真实跑顶层图。
    """

    def __init__(self, sessionmaker, deps, checkpoint_manager) -> None:
        self.sessionmaker = sessionmaker
        self.deps = deps
        self.runner = ResearchOrchestrationRunner(sessionmaker, checkpoint_manager, deps)
        self.manager = ResearchOrchestrationExecutionManager(self.runner)
        self.service = ResearchOrchestrationService(
            sessionmaker,
            deps.plan_service,
            stage5_runner=deps.stage5_runner,
            orchestration_runner=self.runner,
            execution_manager=self.manager,
        )

    async def start(self, task_id):
        """一键入口（POST /tasks/{id}/orchestrations 语义）→ 返回 outcome（后台已调度）。"""
        return await self.service.prepare_orchestration_start(task_id)

    async def wait_idle(self, orchestration_id: UUID, *, message: str) -> None:
        """等待后台任务完成（registry 清空 → 终态已投影）。"""

        async def _idle() -> bool:
            return not self.manager.is_scheduled(orchestration_id)

        await _wait_until(_idle, message=message)

    async def close(self) -> None:
        await self.manager.close()


class _StatefulPreparation:
    """模拟 production prepare（补资料后重新评估 readiness）。

    `sources_available` False → 持续 not_ready（K1 waiting_manual）；用户补 source
    （翻转 flag，等价 source-records upload 成功入库）→ 后续 prepare 全部 ready
    → K1 resume 同线程重跑 prepare → Stage4/Stage5 → completed。
    """

    def __init__(self, request) -> None:
        self._request = request
        self.sources_available = False
        self.calls = 0
        self._not_ready_codes = ("annual_report_financial", "audit_report")

    async def prepare_research(self, research_plan_id: UUID) -> ResearchPreparationResult:
        self.calls += 1
        if not self.sources_available:
            return ResearchPreparationResult(
                research_plan_id=research_plan_id,
                resolved=(),
                module_inputs=(),
                missing_needs=tuple(
                    MissingResearchNeed(code, "document", MissingReasonCode.NOT_FOUND, "missing")
                    for code in self._not_ready_codes
                ),
                ready_for_analysis=False,
                stage4_request=None,
            )
        return ResearchPreparationResult(
            research_plan_id=research_plan_id,
            resolved=(),
            module_inputs=(),
            missing_needs=(),
            ready_for_analysis=True,
            stage4_request=self._request,
        )


class _ScriptedBackflowExecutor:
    """K2 模拟 executor：call 1 → manual_required(source_acquisition_required)；
    call 2+ → 返回预置 new_evidence_card_batch（最后重复）。

    首启 backflow 轮次缺 eligible source（补资料前）→ 稳定 manual reason；用户补
    source 后 resume 重跑同一轮 → 产出新卡（progress）。只投影 application
    output（同 `_FakeBackflowExecutor`，0 真实检索链；真实链已由
    test_research_backflow_executor.py / stage5 E2E 覆盖）。
    """

    def __init__(self, *, manual_need_code: str, new_card_batch) -> None:
        self._manual_need_code = manual_need_code
        self._new_card_batch = tuple(new_card_batch)
        self.calls = 0

    async def execute_supplemental_research(self, verified_request, plan_payload):
        self.calls += 1
        if self.calls == 1:
            attempt = ResearchBackflowNeedExecution(
                need_code=self._manual_need_code,
                status=RESEARCH_BACKFLOW_NEED_STATUS_MANUAL_REQUIRED,
                created_evidence_card_ids=(),
                replayed_evidence_card_ids=(),
                manual_required_reason=RESEARCH_BACKFLOW_MANUAL_REASON_SOURCE_ACQUISITION,
            )
            return ResearchBackflowExecutionResult(
                attempts=(attempt,),
                new_evidence_card_ids=(),
                replayed_evidence_card_ids=(),
                all_manual_required=True,
                resolved_need_codes=(),
            )
        return ResearchBackflowExecutionResult(
            attempts=(),
            new_evidence_card_ids=self._new_card_batch,
            replayed_evidence_card_ids=(),
            all_manual_required=False,
            resolved_need_codes=(self._manual_need_code,),
        )


# ---------------------------------------------------------------- Case 1


async def test_product_case1_one_click_full_chain(env, monkeypatch, connection_uri) -> None:
    """Case 1：一键入口 → 后台真实顶层图 → Stage4 → Synthesis → Stage5 → audit pass
    → completed（单次 run，无重复产物）；完成后再次一键入口 → 同一 orchestration。"""
    ids = await _seed_worker_inputs(env, monkeypatch)
    request = _request(env, ids)
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]
    company_id = env["company_id"]

    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        deps = _orchestration_deps(
            sessionmaker, manager, request, audit_model=_audit_model(pass_decision)
        )
        harness = _ProductHarness(sessionmaker, deps, manager)

        outcome = await harness.start(task_id)
        assert outcome.created is True
        assert outcome.scheduled is True
        assert outcome.orchestration.attempt_no == 1
        o1 = outcome.orchestration.orchestration_id

        await harness.wait_idle(o1, message="一键入口后台任务未在超时前完成")
        row = await _get_orchestration_row(sessionmaker, o1)
        assert row["status"] == "completed"
        assert row["current_phase"] == "completed"
        assert row["error_code"] is None

        # 恰好 1 stage4 + 1 stage5 run，全部 completed（无重复产物）。
        runs = await _runs_for_task(sessionmaker, task_id)
        assert len(runs) == 2
        assert {r["graph_name"] for r in runs} == {"stage4_analysis", "stage5_report"}
        assert all(r["status"] == "completed" for r in runs)
        child4 = await _get_child(sessionmaker, o1, "stage4")
        child5 = await _get_child(sessionmaker, o1, "stage5")
        assert child4 is not None and child5 is not None

        # 真实产物：5 claims + 1 synthesis + 1 report + 0 人工决策。
        assert await _claim_count_for_company(sessionmaker, company_id) == 5
        s_runs, s_results = await _synthesis_counts(sessionmaker)
        assert (s_runs, s_results) == (1, 1)
        assert await _count(sessionmaker, "reports") == 1
        assert await _count(sessionmaker, "human_review_decisions") == 0

        # 产品 re-entry 幂等：completed → 再次一键入口返回同一 orchestration。
        outcome2 = await harness.start(task_id)
        assert outcome2.created is False
        assert outcome2.scheduled is False
        assert outcome2.orchestration.orchestration_id == o1
        assert await _count(sessionmaker, "research_orchestration_runs") == 1
    finally:
        await harness.close()
        await manager.close()


# ---------------------------------------------------------------- Case 2 (K1)


async def test_product_case2_waiting_manual_resume(env, monkeypatch, connection_uri) -> None:
    """Case 2（K1）：首启 not_ready → fulfill 后仍 not_ready → waiting_manual
    （0 child）；用户补 source → `resume_after_source_acquisition` → **同一
    orchestration + 同一顶层线程** → prepare 重算 ready → Stage4/Stage5 →
    completed（无重复 orchestration / child）。"""
    ids = await _seed_worker_inputs(env, monkeypatch)
    request = _request(env, ids)
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]
    company_id = env["company_id"]

    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        prep = _StatefulPreparation(request)
        deps = _orchestration_deps(
            sessionmaker,
            manager,
            request,
            audit_model=_audit_model(pass_decision),
            preparation=prep,
        )
        harness = _ProductHarness(sessionmaker, deps, manager)

        outcome = await harness.start(task_id)
        o1 = outcome.orchestration.orchestration_id
        await harness.wait_idle(o1, message="waiting_manual 未在超时前到达")
        row = await _get_orchestration_row(sessionmaker, o1)
        assert row["status"] == "waiting_human"
        assert row["current_phase"] == "waiting_manual"
        # 0 个 child run（waiting_manual 不做研究）。
        assert await _count(sessionmaker, "workflow_runs") == 0
        # 产品投影：missing_need_codes + phase（spec O）。
        proj = await harness.service.get_orchestration(o1)
        assert proj.current_phase == "waiting_manual"
        assert proj.missing_need_codes == ["annual_report_financial", "audit_report"]
        assert proj.manual_reason is None
        # 未进入 backflow：checkpoint 无 backflow_round 键 → 投影 None。
        assert proj.backflow_round is None

        # 用户补 source（source-records upload 成功入库的等价语义）→ K1 resume。
        prep.sources_available = True
        result = await harness.service.resume_after_source_acquisition(o1)
        assert result.orchestration_id == o1
        await harness.wait_idle(o1, message="K1 resume 未在超时前完成")
        row = await _get_orchestration_row(sessionmaker, o1)
        assert row["status"] == "completed"
        assert row["current_phase"] == "completed"

        # 同一 orchestration / 同一顶层线程：1 行 orchestration、attempt=1、无 retry。
        assert await _count(sessionmaker, "research_orchestration_runs") == 1
        assert row["attempt_no"] == 1
        assert row["retry_of_orchestration_id"] is None
        # resume 后 stage4/stage5 attempt 1 恰好各 1 条 run，全部 completed。
        runs = await _runs_for_task(sessionmaker, task_id)
        assert len(runs) == 2
        assert all(r["status"] == "completed" for r in runs)
        child4 = await _get_child(sessionmaker, o1, "stage4")
        child5 = await _get_child(sessionmaker, o1, "stage5")
        assert child4 is not None and child5 is not None
        assert await _claim_count_for_company(sessionmaker, company_id) == 5
        assert await _count(sessionmaker, "reports") == 1
    finally:
        await harness.close()
        await manager.close()


# ---------------------------------------------------------------- Case 3 (K2)


async def test_product_case3_backflow_resume_same_request(env, monkeypatch, connection_uri) -> None:
    """Case 3（K2）：research_backflow + reason=source_acquisition_required → 补资料 →
    `resume_after_source_acquisition` → **同 research_request_id + 同 backflow_round**
    （不 round+1、replay 同 plan）→ 重跑 execute_supplemental_research 产出新卡 →
    Stage4/Stage5 attempt2 → fulfill → audit pass → completed。"""
    ids = await _seed_worker_inputs(env, monkeypatch)
    extra = await _seed_claim_doc_card(
        env,
        statement="贵州茅台 2026 年经销商渠道补充证据。",
        source_url="https://www.xinhuanet.com/2026/0810/product-extra.htm",
        research_question=_QUESTION,
    )
    request = _request(env, ids)
    request2 = _with_extra_card(request, extra["evidence_card_id"])
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]
    company_id = env["company_id"]

    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        executor = _ScriptedBackflowExecutor(
            manual_need_code="insufficient_evidence",
            new_card_batch=[extra["evidence_card_id"]],
        )
        # prepare 调用序：首启 3 次（prepare + ensure_stage4_child +
        # run_or_resume_stage4 都用 base）→ resume 后 2 次（prepare_updated_analysis
        # + run_or_resume_stage4 用 request2）。
        deps = _orchestration_deps(
            sessionmaker,
            manager,
            request,
            audit_model=_audit_model(_SequencedAuditDecision(research_decision, pass_decision)),
            prep_outcomes=[
                (True, request, []),
                (True, request, []),
                (True, request, []),
                (True, request2, []),
            ],
            models=_ref_aware_models(),
            backflow_executor=executor,
        )
        harness = _ProductHarness(sessionmaker, deps, manager)

        outcome = await harness.start(task_id)
        o1 = outcome.orchestration.orchestration_id
        await harness.wait_idle(o1, message="backflow manual 未在超时前到达")
        row = await _get_orchestration_row(sessionmaker, o1)
        assert row["status"] == "waiting_human"
        assert row["current_phase"] == "research_backflow"
        proj = await harness.service.get_orchestration(o1)
        assert proj.manual_reason == RESEARCH_BACKFLOW_MANUAL_REASON_SOURCE_ACQUISITION
        assert proj.backflow_round == 1
        request_id_before = proj.research_request_id
        assert request_id_before is not None
        assert executor.calls == 1

        # 用户补 source → K2 resume（同 request + 同 round）。
        result = await harness.service.resume_after_source_acquisition(o1)
        assert result.orchestration_id == o1
        await harness.wait_idle(o1, message="K2 resume 未在超时前完成")
        row = await _get_orchestration_row(sessionmaker, o1)
        assert row["status"] == "completed"
        assert row["current_phase"] == "completed"

        proj_after = await harness.service.get_orchestration(o1)
        # 同 research_request_id + 同 backflow_round（不 round+1，K2）。
        assert proj_after.research_request_id == request_id_before
        assert proj_after.backflow_round == 1
        assert proj_after.manual_reason is None
        # 同 request → 同 plan（create_or_get replay 不新建）。
        assert await _count(sessionmaker, "research_backflow_requests") == 1
        assert await _count(sessionmaker, "research_backflow_plans") == 1
        assert await _count(sessionmaker, "research_backflow_fulfillments") == 1
        assert executor.calls == 2

        # Stage4/Stage5 attempt 1 + attempt 2；attempt2 记录 source_research_request_id。
        child4_1 = await _get_child(sessionmaker, o1, "stage4", attempt_no=1)
        child4_2 = await _get_child(sessionmaker, o1, "stage4", attempt_no=2)
        child5_1 = await _get_child(sessionmaker, o1, "stage5", attempt_no=1)
        child5_2 = await _get_child(sessionmaker, o1, "stage5", attempt_no=2)
        assert child4_1 is not None and child5_1 is not None
        assert child4_2 is not None and child5_2 is not None
        assert child4_1["source_research_request_id"] is None
        assert child4_2["source_research_request_id"] is not None
        assert child5_2["source_research_request_id"] is not None
        assert child4_1["run_id"] != child4_2["run_id"]
        assert child5_1["run_id"] != child5_2["run_id"]

        # 无重复 orchestration；4 条 run（stage4 x2 + stage5 x2）全部 completed。
        assert await _count(sessionmaker, "research_orchestration_runs") == 1
        runs = await _runs_for_task(sessionmaker, task_id)
        assert len(runs) == 4
        assert all(r["status"] == "completed" for r in runs)
        # 6 = attempt1 的 5 条 claim + attempt2 因纳入 extra 卡产生的 1 条新 biz claim。
        assert await _claim_count_for_company(sessionmaker, company_id) == 6
        s_runs, s_results = await _synthesis_counts(sessionmaker)
        assert (s_runs, s_results) == (2, 2)
    finally:
        await harness.close()
        await manager.close()


# ---------------------------------------------------------------- Case 4


async def test_product_case4_stage5_approve_same_run(env, monkeypatch, connection_uri) -> None:
    """Case 4：awaiting_stage5 → `act_on_orchestration(approve)`（产品人工入口）→
    同一 Stage5 run resume → orchestration completed（无重复 Stage5 / Report）。"""
    ids = await _seed_worker_inputs(env, monkeypatch)
    request = _request(env, ids)
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]

    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        deps = _orchestration_deps(
            sessionmaker,
            manager,
            request,
            audit_model=_audit_model(human_review_decision),
        )
        harness = _ProductHarness(sessionmaker, deps, manager)

        outcome = await harness.start(task_id)
        o1 = outcome.orchestration.orchestration_id
        await harness.wait_idle(o1, message="awaiting_stage5 未在超时前到达")
        row = await _get_orchestration_row(sessionmaker, o1)
        assert row["status"] == "waiting_human"
        assert row["current_phase"] == "awaiting_stage5"
        child5 = await _get_child(sessionmaker, o1, "stage5")
        assert child5 is not None
        stage5_run_id = child5["run_id"]
        assert await _run_status(sessionmaker, UUID(stage5_run_id)) == "waiting_human"

        # 产品人工决策：approve → same Stage5 run resume → completed。
        result = await harness.service.act_on_orchestration(o1, "approve", "审核通过")
        assert result.status == "completed"
        row = await _get_orchestration_row(sessionmaker, o1)
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
        assert await _count(sessionmaker, "research_orchestration_runs") == 1
    finally:
        await harness.close()
        await manager.close()


# ---------------------------------------------------------------- Case 5 (K3)


async def test_product_case5_limit_reached_not_resumable(env, monkeypatch, connection_uri) -> None:
    """Case 5（K3）：research_backflow_limit_reached（MAX rounds=2 不可绕过）→
    `resume_after_source_acquisition` → `ResearchOrchestrationInvalidAction`；
    orchestration 原样保留（waiting_human / research_backflow / limit reason）。"""
    ids = await _seed_worker_inputs(env, monkeypatch)
    extra_a = await _seed_claim_doc_card(
        env,
        statement="贵州茅台 2026 年渠道证据 A。",
        source_url="https://www.xinhuanet.com/2026/0810/productA.htm",
        research_question=_QUESTION,
    )
    extra_b = await _seed_claim_doc_card(
        env,
        statement="贵州茅台 2026 年渠道证据 B。",
        source_url="https://www.xinhuanet.com/2026/0810/productB.htm",
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
        executor = _FakeBackflowExecutor(
            new_card_batches=[
                (extra_a["evidence_card_id"],),
                (extra_b["evidence_card_id"],),
            ]
        )
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
        harness = _ProductHarness(sessionmaker, deps, manager)

        outcome = await harness.start(task_id)
        o1 = outcome.orchestration.orchestration_id
        await harness.wait_idle(o1, message="limit manual 未在超时前到达")
        row = await _get_orchestration_row(sessionmaker, o1)
        assert row["status"] == "waiting_human"
        assert row["current_phase"] == "research_backflow"
        proj = await harness.service.get_orchestration(o1)
        assert proj.manual_reason == RESEARCH_BACKFLOW_LIMIT_REACHED
        assert proj.backflow_round == 2
        assert executor.calls == 2

        # K3：MAX rounds 不可绕过 → resume 被拒绝，不调度任何后台任务。
        with pytest.raises(ResearchOrchestrationInvalidAction):
            await harness.service.resume_after_source_acquisition(o1)
        assert not harness.manager.is_scheduled(o1)
        row = await _get_orchestration_row(sessionmaker, o1)
        assert row["status"] == "waiting_human"
        assert row["current_phase"] == "research_backflow"
        assert await _count(sessionmaker, "research_orchestration_runs") == 1
    finally:
        await harness.close()
        await manager.close()
