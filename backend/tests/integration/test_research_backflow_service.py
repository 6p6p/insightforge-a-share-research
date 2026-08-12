"""Research backflow service E2E + 负向 tests (stage 5E.2B, spec F–S/U/V).

真实 PostgreSQL + Fake Writer / Fake Auditor + 真实 LangGraph（PG Checkpointer），
全程**零真实 DeepSeek / 0 LLM / 0 检索 / 0 Chroma / 0 Stage4 Analyst 重跑**——
Stage5 只做可验证 research handoff，并消费 upstream 返回的新 SynthesisResult。

覆盖（spec S 负向矩阵 + 4 条路径）：
- 路径 A（direct research）：route=research → graph 节点自动创建 request →
  `create_or_get_request` 幂等 replay → identity/cutoff/payload 断言 →
  `verify_research_request_integrity` 完整重建；
- 路径 B（human research）：human_review → resume research → request 带
  human_decision_id，payload 恒含 `human_requested_research`；
- 路径 C（fulfillment + continuation + 续跑）：upstream 新综合（_two_theme_models，
  同 company/question/cutoff）→ `fulfill_request` replay → verify → continuation
  request → **新** Stage5 run 以 pass audit finalize（spec O，4 条路径闭环）；
- 负向矩阵（spec S）：InvalidRun / Stage5ContextMissing / NotResearchTerminal /
  InvalidState / IllegalTrigger（spec G 纯逻辑）/ RequestNotFound /
  FulfillmentNotFound / ContinuationMismatch×3 / NoProgress×2 / AlreadyFulfilled /
  IntegrityError（request tamper，不自动 repair）。
"""

import hashlib
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.research_backflow.contracts import (
    MAX_QUERIES_PER_NEED,
    RESEARCH_BACKFLOW_PLAN_SCHEMA_VERSION,
    SUPPLEMENTAL_RESEARCH_STRATEGY_NAME,
)
from app.research_backflow.errors import (
    ResearchBackflowAlreadyFulfilled,
    ResearchBackflowContinuationMismatch,
    ResearchBackflowFulfillmentNotFound,
    ResearchBackflowIllegalTrigger,
    ResearchBackflowIntegrityError,
    ResearchBackflowInvalidRun,
    ResearchBackflowInvalidState,
    ResearchBackflowNoProgress,
    ResearchBackflowNotResearchTerminal,
    ResearchBackflowRequestNotFound,
    ResearchBackflowStage5ContextMissing,
)
from app.research_backflow.service import ResearchBackflowService
from app.services.source_registry_service import SourceRegistryService
from app.stage4.contracts import Stage4WorkflowRequest
from app.stage4.runner import Stage4WorkflowRunner
from app.stage5.contracts import (
    STAGE5_TERMINAL_FINALIZE,
    STAGE5_TERMINAL_RESEARCH_REQUIRED,
)
from app.stage5.errors import Stage5InvalidState
from app.stage5.nodes import make_create_research_backflow_request_node
from app.stage5.runner import Stage5WorkflowRunner
from app.storage.raw_store import LocalRawArtifactStore
from app.workflows.checkpoint import LangGraphCheckpointManager
from tests.audit.fakes import FakeAuditModel, pass_decision
from tests.draft_section.fakes import FakeDraftSectionModel, valid_decision_for
from tests.integration.test_draft_section_service import (
    _build_deps,
    _good_models,
    _two_theme_models,
)
from tests.integration.test_report_audit_service import (
    human_review_decision,
    research_decision,
)
from tests.integration.test_revision_service import _cleanup_with_revisions
from tests.integration.test_stage4_workflow import (
    _request as _stage4_request,
)
from tests.integration.test_stage4_workflow import (
    _seed_claim_doc_card,
    _seed_research_task,
    _seed_worker_inputs,
)
from tests.integration.test_stage5_workflow import (
    _AS_OF,
    _QUESTION,
    _request,
    _run_count,
    _seed_synthesis,
    _stage5_deps,
)
from tests.integration.test_valuation_claim_service import _seed_company
from tests.revision.fakes import FakeRevisionWriterModel

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()


def _hex64() -> str:
    return hashlib.sha256(uuid4().bytes).hexdigest()


# ---------------------------------------------------------------- fixtures


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
async def env(tmp_path, sessionmaker, monkeypatch) -> dict:
    raw_store = LocalRawArtifactStore(root=tmp_path / "raw", max_bytes=1024 * 1024)
    await _cleanup_with_revisions(sessionmaker)
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
    await _cleanup_with_revisions(sessionmaker)


async def _insert_stage5_run(sessionmaker, task_id: UUID) -> UUID:
    """直接 SQL 插入一条无 checkpoint 的 stage5 run（负向场景，避开完整执行）。"""
    run_id = uuid4()
    async with sessionmaker() as session:
        await session.execute(
            text(
                "INSERT INTO workflow_runs "
                "(run_id, task_id, thread_id, graph_name, graph_version, status) "
                "VALUES (CAST(:rid AS uuid), CAST(:tid AS uuid), "
                " :thread_id, 'stage5_report', '1', 'completed')"
            ).bindparams(rid=run_id, tid=task_id, thread_id=str(run_id))
        )
        await session.commit()
    return run_id


async def _run_stage4_request(env, connection_uri, request, models) -> dict:
    """单次 Stage4 graph 执行（同一批 worker inputs 上可重复跑）。"""
    deps = _build_deps(env["sessionmaker"], models)
    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        runner = Stage4WorkflowRunner(env["sessionmaker"], manager, deps)
        run = await runner.create_stage4_run(request)
        return await runner.execute_stage4(run.run_id, request)
    finally:
        await manager.close()


async def _seed_two_syntheses(env, monkeypatch, connection_uri) -> tuple[UUID, UUID]:
    """seed inputs 一次 + 新增一条 evidence → 两个不同 synthesis result。

    `_seed_synthesis`（`_run_stage4_to_result`）每次重 seed source records 且
    artifact_id 由内容确定 → 同 URL 同 artifact → 第二次必然撞 UNIQUE。这里只 seed
    一次 inputs；第二轮把 biz work item 换成**新来源**的 evidence card（不同
    URL → 新 source record；新 card id 进入 claim supports → 新 claim → 新 input
    set → 新 SynthesisRun fingerprint，spec M no-progress 双条件都满足）。

    source 用 `_good_models`（第一轮 request），new 用 `_two_theme_models` 且 biz
    卡替换——同一 research question / company / analysis_as_of（spec L identity）。
    """
    ids = await _seed_worker_inputs(env, monkeypatch)
    request = _stage4_request(env, ids)
    source = await _run_stage4_request(env, connection_uri, request, _good_models())

    # upstream 返回的新证据：不同来源 + 不同主张。
    extra = await _seed_claim_doc_card(
        env,
        statement="2024年公司经营现金流净额同比增长20%。",
        source_url="https://www.xinhuanet.com/2026/0809/s4cash.htm",
    )
    items = []
    for item in request.analysis_work_items:
        if item.item_id == "biz":
            items.append(item.model_copy(update={"evidence_card_ids": [extra["evidence_card_id"]]}))
        else:
            items.append(item)
    request_b = Stage4WorkflowRequest(
        task_id=request.task_id,
        company_id=request.company_id,
        research_question=request.research_question,
        analysis_as_of=request.analysis_as_of,
        analysis_work_items=items,
    )
    new = await _run_stage4_request(env, connection_uri, request_b, _two_theme_models())
    return UUID(source["synthesis_result_id"]), UUID(new["synthesis_result_id"])


# ---------------------------------------------------------------- 路径 A：direct research


async def test_research_request_auto_created_and_replayed(env, monkeypatch, connection_uri) -> None:
    """route=research → graph 节点自动创建 request；replay 幂等；identity/cutoff 绑定。"""
    synthesis_result_id = await _seed_synthesis(env, monkeypatch, connection_uri)
    request = _request(env, synthesis_result_id)
    deps = _stage5_deps(
        env["sessionmaker"],
        draft_model=FakeDraftSectionModel(decision_factory=valid_decision_for),
        audit_model=FakeAuditModel(decision_factory=research_decision),
        revision_model=FakeRevisionWriterModel(),
    )
    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        runner = Stage5WorkflowRunner(env["sessionmaker"], manager, deps)
        run = await runner.create_stage5_run(request)
        result = await runner.execute_stage5(run.run_id, request)
    finally:
        await manager.close()

    assert result["terminal"] == STAGE5_TERMINAL_RESEARCH_REQUIRED
    assert result["research_request_id"] is not None
    # 节点在 execute 期间已持久化请求（幂等 create_or_get）。
    assert await _run_count(env["sessionmaker"], "research_backflow_requests") == 1

    service = deps.research_backflow_service
    req = await service.create_or_get_request(run.run_id)
    assert req.replayed is True
    assert req.research_request_id == UUID(result["research_request_id"])
    assert req.source_stage5_run_id == run.run_id
    assert req.source_report_id == UUID(result["report_id"])
    assert req.human_decision_id is None

    # 身份 / cutoff 只从 Report→Outline→Synthesis chain 恢复（caller 不能提供）。
    assert req.company_id == env["company_id"]
    assert req.analysis_as_of == _AS_OF
    assert len(req.research_question_sha256) == 64

    # payload 只含 5 个派生键；research action 的 research_need_codes 保留。
    assert set(req.request_payload.keys()) == {
        "review_issue_ids",
        "target_section_ids",
        "related_claim_ids",
        "related_evidence_card_ids",
        "research_need_codes",
    }
    assert req.request_payload["research_need_codes"] == ["missing_support"]

    # read-side 完整重建（重放校验通过）。
    verified = await service.verify_research_request_integrity(req.research_request_id)
    assert verified.verified_source_synthesis.synthesis_result_id == synthesis_result_id
    assert verified.verified_action.action_type == "research"
    assert verified.verified_decision is None
    assert (
        verified.research_question_sha256
        == verified.verified_source_synthesis.research_question_sha256
    )
    assert verified.analysis_as_of == verified.verified_source_synthesis.analysis_as_of


# ---------------------------------------------------------------- 补充研究计划（7A.2B.3 spec K）


async def test_research_backflow_plan_created_and_replayed(
    env, monkeypatch, connection_uri
) -> None:
    """supplemental plan：确定性派生 + create_or_get replay + tamper → IntegrityError。

    audit issue（unsupported_by_evidence，白名单）→ 恰好一个 need_spec；query /
    allowed_source_types 非空（冻结模板 + 真实 source 词表）；replay 幂等同指纹；
    直接改 DB payload → 重放校验失败（**不自动 repair**）。
    """
    synthesis_result_id = await _seed_synthesis(env, monkeypatch, connection_uri)
    request = _request(env, synthesis_result_id)
    deps = _stage5_deps(
        env["sessionmaker"],
        draft_model=FakeDraftSectionModel(decision_factory=valid_decision_for),
        audit_model=FakeAuditModel(decision_factory=research_decision),
        revision_model=FakeRevisionWriterModel(),
    )
    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        runner = Stage5WorkflowRunner(env["sessionmaker"], manager, deps)
        run = await runner.create_stage5_run(request)
        result = await runner.execute_stage5(run.run_id, request)
    finally:
        await manager.close()
    assert result["terminal"] == STAGE5_TERMINAL_RESEARCH_REQUIRED

    service = deps.research_backflow_service
    req = await service.create_or_get_request(run.run_id)

    plan = await service.create_or_get_plan(req.research_request_id)
    assert plan.replayed is False
    assert plan.plan_schema_version == RESEARCH_BACKFLOW_PLAN_SCHEMA_VERSION
    assert plan.strategy_name == SUPPLEMENTAL_RESEARCH_STRATEGY_NAME
    assert len(plan.plan_fingerprint) == 64
    assert plan.plan_payload["max_queries_per_need"] == MAX_QUERIES_PER_NEED
    # unsupported_by_evidence ∈ 白名单 → 恰好一个 need_spec。
    assert [spec["need_code"] for spec in plan.plan_payload["need_specs"]] == [
        "unsupported_by_evidence"
    ]
    spec = plan.plan_payload["need_specs"][0]
    assert spec["retrieval_queries"], "冻结 query 模板非空"
    assert spec["allowed_source_types"], "真实 source 词表非空"

    # 数据库落一行。
    assert await _run_count(env["sessionmaker"], "research_backflow_plans") == 1

    # replay 幂等：同一 plan_id / 同一指纹。
    again = await service.create_or_get_plan(req.research_request_id)
    assert again.replayed is True
    assert again.backflow_plan_id == plan.backflow_plan_id
    assert again.plan_fingerprint == plan.plan_fingerprint

    # tamper：直接改 DB payload → 重放校验失败（0 自动 repair）。
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE research_backflow_plans SET plan_payload = JSONB_SET("
                "plan_payload, '{max_queries_per_need}', '9'::jsonb) "
                "WHERE research_backflow_request_id = CAST(:rid AS uuid)"
            ).bindparams(rid=req.research_request_id)
        )
        await session.commit()
    with pytest.raises(ResearchBackflowIntegrityError):
        await service.create_or_get_plan(req.research_request_id)


# ---------------------------------------------------------------- 路径 B：human research


async def test_human_research_request_created_via_resume(env, monkeypatch, connection_uri) -> None:
    """human_review → resume research → request 带 human_decision_id。"""
    synthesis_result_id = await _seed_synthesis(env, monkeypatch, connection_uri)
    request = _request(env, synthesis_result_id)
    deps = _stage5_deps(
        env["sessionmaker"],
        draft_model=FakeDraftSectionModel(decision_factory=valid_decision_for),
        audit_model=FakeAuditModel(decision_factory=human_review_decision),
        revision_model=FakeRevisionWriterModel(),
    )
    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        runner = Stage5WorkflowRunner(env["sessionmaker"], manager, deps)
        run = await runner.create_stage5_run(request)
        result = await runner.execute_stage5(run.run_id, request)
        assert result["route"] == "human_review"
        assert (await runner.get_run(run.run_id)).status.value == "waiting_human"

        result = await runner.resume_stage5_human(run.run_id, "research", comment=" 需要补充证据 ")
    finally:
        await manager.close()

    assert result["terminal"] == STAGE5_TERMINAL_RESEARCH_REQUIRED
    assert result["research_request_id"] is not None
    assert await _run_count(env["sessionmaker"], "research_backflow_requests") == 1

    service = deps.research_backflow_service
    req = await service.create_or_get_request(run.run_id)
    assert req.replayed is True
    assert req.human_decision_id is not None

    verified = await service.verify_research_request_integrity(req.research_request_id)
    assert verified.verified_action.action_type == "human_review"
    assert verified.verified_decision.decision == "research"
    # human research 恒含 human_requested_research code（spec H）。
    assert "human_requested_research" in req.request_payload["research_need_codes"]


# --------------------------------------------------------------- 路径 C：fulfillment + continuation


async def test_fulfillment_replay_continuation_finalize(env, monkeypatch, connection_uri) -> None:
    """research request → upstream 新综合 → fulfill → continuation → 新 run finalize。"""
    synthesis_result_id, new_result_id = await _seed_two_syntheses(env, monkeypatch, connection_uri)
    assert new_result_id != synthesis_result_id
    request = _request(env, synthesis_result_id)
    deps = _stage5_deps(
        env["sessionmaker"],
        draft_model=FakeDraftSectionModel(decision_factory=valid_decision_for),
        audit_model=FakeAuditModel(decision_factory=research_decision),
        revision_model=FakeRevisionWriterModel(),
    )
    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        runner = Stage5WorkflowRunner(env["sessionmaker"], manager, deps)
        run = await runner.create_stage5_run(request)
        await runner.execute_stage5(run.run_id, request)
    finally:
        await manager.close()
    service = deps.research_backflow_service
    req = await service.create_or_get_request(run.run_id)

    # upstream 返回新综合：不同分析输出 → 新 result；同 company/question/cutoff。
    # fulfillment：首次创建；同 request+result 再 fulfill → replay 同一行。
    f1 = await service.fulfill_request(req.research_request_id, new_result_id)
    assert f1.replayed is False
    assert f1.new_synthesis_result_id == new_result_id
    f2 = await service.fulfill_request(req.research_request_id, new_result_id)
    assert f2.replayed is True
    assert f2.fulfillment_id == f1.fulfillment_id

    # read-side 完整重建。
    vf = await service.verify_research_fulfillment_integrity(f1.fulfillment_id)
    assert vf.verified_new_synthesis.synthesis_result_id == new_result_id
    assert vf.verified_request.research_request_id == req.research_request_id

    # continuation：任务 / 公司 / 问题 / cutoff 与源链一致；synthesis 是新 result。
    cont = await service.build_stage5_continuation_request(f1.fulfillment_id)
    assert cont.task_id == env["task_id"]
    assert cont.company_id == env["company_id"]
    assert cont.research_question == _QUESTION
    assert cont.analysis_as_of == _AS_OF
    assert cont.synthesis_result_id == new_result_id

    # 4th path：用 continuation request 开**新** Stage5 run → 本轮 audit=pass → finalize。
    deps2 = _stage5_deps(
        env["sessionmaker"],
        draft_model=FakeDraftSectionModel(decision_factory=valid_decision_for),
        audit_model=FakeAuditModel(decision_factory=pass_decision),
        revision_model=FakeRevisionWriterModel(),
    )
    manager2 = LangGraphCheckpointManager(connection_uri)
    await manager2.setup()
    try:
        runner2 = Stage5WorkflowRunner(env["sessionmaker"], manager2, deps2)
        run2 = await runner2.create_stage5_run(cont)
        result2 = await runner2.execute_stage5(run2.run_id, cont)
    finally:
        await manager2.close()

    assert run2.run_id != run.run_id
    assert result2["terminal"] == STAGE5_TERMINAL_FINALIZE
    assert (await runner2.get_run(run2.run_id)).status.value == "completed"


# ---------------------------------------------------------------- 负向矩阵（spec S）


async def test_request_negative_matrix(env, monkeypatch, connection_uri) -> None:
    """一个真实 research request 上覆盖全部负向守卫（0 write / 不自动 repair）。"""
    synthesis_result_id, new_result_id = await _seed_two_syntheses(env, monkeypatch, connection_uri)
    request = _request(env, synthesis_result_id)
    deps = _stage5_deps(
        env["sessionmaker"],
        draft_model=FakeDraftSectionModel(decision_factory=valid_decision_for),
        audit_model=FakeAuditModel(decision_factory=research_decision),
        revision_model=FakeRevisionWriterModel(),
    )
    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        runner = Stage5WorkflowRunner(env["sessionmaker"], manager, deps)
        run = await runner.create_stage5_run(request)
        result = await runner.execute_stage5(run.run_id, request)
    finally:
        await manager.close()
    service = deps.research_backflow_service
    req = await service.create_or_get_request(run.run_id)
    assert result["research_request_id"] is not None

    source_verified = (
        await service.verify_research_request_integrity(req.research_request_id)
    ).verified_source_synthesis

    real_verify = service._synthesis_analysis.verify_result_integrity

    def _different(**replacements):
        """fabricate 一个与 source 真正不同的 SynthesisResult（可注入具体差异字段）。"""
        return replace(
            source_verified,
            synthesis_result_id=uuid4(),
            synthesis_fingerprint=_hex64(),
            **replacements,
        )

    fabricated = None  # _mock_verify 在调用时读取闭包最新值

    async def _mock_verify(_result_id):
        return fabricated

    # 1. no-progress branch 1：直接引用 source result → 拒绝。
    with pytest.raises(ResearchBackflowNoProgress):
        await service.fulfill_request(req.research_request_id, synthesis_result_id)

    # 2. continuation mismatch：company / question hash / cutoff 任一不同 → 拒绝。
    for mismatch in (
        _different(company_id=uuid4()),
        _different(research_question_sha256=_hex64()),
        _different(analysis_as_of=date(2020, 1, 1)),
    ):
        fabricated = mismatch
        service._synthesis_analysis.verify_result_integrity = _mock_verify
        try:
            with pytest.raises(ResearchBackflowContinuationMismatch):
                await service.fulfill_request(req.research_request_id, mismatch.synthesis_result_id)
        finally:
            service._synthesis_analysis.verify_result_integrity = real_verify

    # 3. no-progress branch 2：新 result id 但复用 source run（同 fingerprint）。
    fabricated = replace(source_verified, synthesis_result_id=uuid4())
    service._synthesis_analysis.verify_result_integrity = _mock_verify
    try:
        with pytest.raises(ResearchBackflowNoProgress):
            await service.fulfill_request(req.research_request_id, fabricated.synthesis_result_id)
    finally:
        service._synthesis_analysis.verify_result_integrity = real_verify

    # 4. 真实 fulfillment：首次创建；同 request+result → replay 同一行。
    f = await service.fulfill_request(req.research_request_id, new_result_id)
    assert f.replayed is False
    f2 = await service.fulfill_request(req.research_request_id, new_result_id)
    assert f2.replayed is True
    assert f2.fulfillment_id == f.fulfillment_id

    vf = await service.verify_research_fulfillment_integrity(f.fulfillment_id)
    assert vf.verified_new_synthesis.synthesis_result_id == new_result_id

    # 5. already-fulfilled：request 已兑现且 result 不同 → 拒绝（不覆盖历史）。
    fabricated = _different()
    service._synthesis_analysis.verify_result_integrity = _mock_verify
    try:
        with pytest.raises(ResearchBackflowAlreadyFulfilled):
            await service.fulfill_request(req.research_request_id, fabricated.synthesis_result_id)
    finally:
        service._synthesis_analysis.verify_result_integrity = real_verify

    # 6. read-side not-found。
    with pytest.raises(ResearchBackflowRequestNotFound):
        await service.verify_research_request_integrity(uuid4())
    with pytest.raises(ResearchBackflowFulfillmentNotFound):
        await service.verify_research_fulfillment_integrity(uuid4())

    # 7. request tamper：改写持久化字段 → 重放校验拒绝（不自动 repair）。
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE research_backflow_requests SET analysis_as_of = CAST(:d AS date) "
                "WHERE research_request_id = :rid"
            ).bindparams(d=date(2020, 1, 1), rid=req.research_request_id)
        )
        await session.commit()
    with pytest.raises(ResearchBackflowIntegrityError):
        await service.verify_research_request_integrity(req.research_request_id)


# ---------------------------------------------------------------- 负向：run / trigger 守卫


async def test_invalid_run_context_and_terminal(env, monkeypatch, connection_uri) -> None:
    """InvalidRun / Stage5ContextMissing / NotResearchTerminal（无 checkpoint 场景）。"""
    deps = _stage5_deps(
        env["sessionmaker"],
        draft_model=FakeDraftSectionModel(decision_factory=valid_decision_for),
        audit_model=FakeAuditModel(decision_factory=pass_decision),
        revision_model=FakeRevisionWriterModel(),
    )
    service = ResearchBackflowService(
        env["sessionmaker"], deps.review_action_service, deps.report_service
    )

    # InvalidRun：run 不存在。
    with pytest.raises(ResearchBackflowInvalidRun):
        await service.create_or_get_request(uuid4())

    # Stage5ContextMissing：service 未绑定 Stage5 checkpoint → 无法恢复 final state。
    run_id = await _insert_stage5_run(env["sessionmaker"], env["task_id"])
    with pytest.raises(ResearchBackflowStage5ContextMissing):
        await service.create_or_get_request(run_id)

    # NotResearchTerminal：bound 但 checkpoint 无 research_required terminal
    # （该 run 无任何 checkpoint → 恢复出空 state → terminal=<missing>）。
    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    service.bind_stage5(manager, deps)
    try:
        with pytest.raises(ResearchBackflowNotResearchTerminal):
            await service.create_or_get_request(run_id)
    finally:
        await manager.close()


async def test_invalid_state_and_illegal_triggers(env, monkeypatch, connection_uri) -> None:
    """InvalidState / IllegalTrigger（spec G 纯逻辑）/ 节点防御性硬边界。"""
    deps = _stage5_deps(
        env["sessionmaker"],
        draft_model=FakeDraftSectionModel(decision_factory=valid_decision_for),
        audit_model=FakeAuditModel(decision_factory=pass_decision),
        revision_model=FakeRevisionWriterModel(),
    )
    service = ResearchBackflowService(
        env["sessionmaker"], deps.review_action_service, deps.report_service
    )

    # _coerce_state_uuid：非法 UUID → InvalidState；None 合法（human_decision_id 可为空）。
    with pytest.raises(ResearchBackflowInvalidState):
        service._coerce_state_uuid({"review_action_id": "not-a-uuid"}, "review_action_id")
    assert service._coerce_state_uuid({"human_decision_id": None}, "human_decision_id") is None

    # 图节点防御：缺 source_stage5_run_id → Stage5InvalidState（run FAILED，不静默改道）。
    node = make_create_research_backflow_request_node(deps)
    with pytest.raises(Stage5InvalidState):
        await node({"synthesis_result_id": str(uuid4())})

    # IllegalTrigger（spec G）：只有 research action（无 decision）或
    # human_review + research decision 合法；其余全部拒绝（0 write）。
    research = SimpleNamespace(action_type="research")
    human_review = SimpleNamespace(action_type="human_review")
    finalize = SimpleNamespace(action_type="finalize")
    research_dec = SimpleNamespace(decision="research")
    approve_dec = SimpleNamespace(decision="approve")
    with pytest.raises(ResearchBackflowIllegalTrigger):
        service._check_legal_trigger(finalize, None)
    with pytest.raises(ResearchBackflowIllegalTrigger):
        service._check_legal_trigger(research, research_dec)
    with pytest.raises(ResearchBackflowIllegalTrigger):
        service._check_legal_trigger(human_review, None)
    with pytest.raises(ResearchBackflowIllegalTrigger):
        service._check_legal_trigger(human_review, approve_dec)
    # 合法路径不抛。
    service._check_legal_trigger(research, None)
    service._check_legal_trigger(human_review, research_dec)
