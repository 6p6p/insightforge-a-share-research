"""Task-level read-only artifact workspace tests (Stage 6B.1).

真实 PostgreSQL + 真实 LangGraph（PG Checkpointer）+ Fake LLM models，全程
**零真实 DeepSeek**。覆盖：

1. **完整链产物**：Stage4 completed → Stage5 → approve → completed 后，
   `TaskArtifactService` 5 个只读方法 + `count_artifacts`；evidence/source 集
   为**任务级**（从 checkpoint 恢复，非 company 全集）；source 投影为
   **dual-origin**（document + macro_observation）；
2. **research backflow canonical lineage（章节 K）**：S1 → Stage5(research) →
   新综合 S2（无匹配 Stage4）→ continuation → run2 finalize；workspace 锚定
   S2 / R2 / run2 的 Audit；analysis 不混 S1；work_items_available=false；
   evidence 不混 S1-only 卡；
3. **macro evidence/source 不因 source_id NULL 被丢弃（章节 L）**；
4. **同公司任务隔离（章节 M）**：两个任务共享同一 company，各自只看到自己的
   evidence / analysis（不按 company_id 全集）；
5. **完整性损坏（章节 D）**：checkpoint 引用的 report 产物损坏 →
   `TaskArtifactIntegrityError`（409），不 repair / 不降级为空；
6. **只读路径 0 LLM（章节 E）**：移除 DEEPSEEK_API_KEY 后生产 DI 构建 + 全部
   读取成功；
7. task 不存在 → `TaskNotFound`；无 run 新任务 → 空 / null + counts 全 0。
"""

from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.errors import TaskArtifactIntegrityError, TaskNotFound
from app.core.runtime import configure_asyncio_runtime
from app.db.models.macro_dataset_snapshot import MacroDatasetSnapshotModel
from app.db.models.macro_observation import MacroObservationModel
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.repositories.evidence_card_repository import EvidenceCardRepository
from app.schemas.artifact import (
    AnalysisArtifactResponse,
    EvidenceArtifactListResponse,
    ReportArtifactResponse,
    ReviewsArtifactResponse,
    SourceArtifactListResponse,
)
from app.services.research_execution_recovery import ResearchExecutionRecoveryCoordinator
from app.services.research_execution_service import ResearchExecutionService
from app.services.source_registry_service import SourceRegistryService
from app.services.task_artifact_service import TaskArtifactService
from app.stage4.contracts import Stage4WorkflowRequest
from app.stage4.runner import Stage4WorkflowRunner
from app.stage5.contracts import Stage5WorkflowRequest
from app.stage5.dependencies import create_stage5_dependencies
from app.stage5.runner import Stage5WorkflowRunner
from app.storage.raw_store import LocalRawArtifactStore
from app.synthesis.service import SynthesisService
from app.workflows.checkpoint import LangGraphCheckpointManager
from tests.audit.fakes import FakeAuditModel, pass_decision
from tests.draft_section.fakes import FakeDraftSectionModel, valid_decision_for
from tests.integration.test_draft_section_service import (
    _build_deps,
    _good_models,
    _two_theme_models,
)
from tests.integration.test_report_audit_service import research_decision
from tests.integration.test_research_execution_recovery import (
    _get_run_status,
    _make_execution,
    _run_stage4_to_completed,
    _wait_for_stage5_waiting_human,
)
from tests.integration.test_revision_service import _cleanup_with_revisions
from tests.integration.test_stage4_workflow import (
    _AS_OF,
    _QUESTION,
    _seed_claim_doc_card,
    _seed_research_task,
    _seed_worker_inputs,
)
from tests.integration.test_stage4_workflow import (
    _request as _stage4_request,
)
from tests.integration.test_stage5_workflow import _stage5_deps
from tests.integration.test_valuation_claim_service import _seed_company
from tests.revision.fakes import FakeRevisionWriterModel

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_WORK_ITEM_EVIDENCE_KEYS = (
    "evidence_card_ids",
    "additional_evidence_ids",
    "macro_driver_evidence_ids",
    "company_evidence_ids",
)


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
async def env(tmp_path, sessionmaker) -> dict:
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


# ---------------------------------------------------------------- helpers


def _make_artifact_service(
    sessionmaker, manager: LangGraphCheckpointManager
) -> TaskArtifactService:
    """Stage 6B.1 构造：from_dependencies（复用 Stage5 DI 的同一批 Services）。

    verify 是 read-only 重放，deps 里的 fake models 不会被调用（0 LLM）。
    """
    deps = _stage5_deps(
        sessionmaker,
        draft_model=FakeDraftSectionModel(decision_factory=valid_decision_for),
        audit_model=FakeAuditModel(decision_factory=pass_decision),
        revision_model=FakeRevisionWriterModel(),
    )
    return TaskArtifactService.from_dependencies(sessionmaker, manager, deps)


async def _run_stage4_graph(env, connection_uri, request: Stage4WorkflowRequest, models) -> UUID:
    """单次 Stage4 graph 执行（可在任意 task / 输入集上重复跑）→ synthesis_result_id。"""
    deps = _build_deps(env["sessionmaker"], models)
    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        runner = Stage4WorkflowRunner(env["sessionmaker"], manager, deps)
        run = await runner.create_stage4_run(request)
        result = await runner.execute_stage4(run.run_id, request)
    finally:
        await manager.close()
    assert result["synthesis_result_id"] is not None
    return UUID(result["synthesis_result_id"])


async def _run_full_chain_to_completed(
    env,
    monkeypatch,
    connection_uri,
) -> tuple[LangGraphCheckpointManager, UUID]:
    """Stage4 completed → Stage5 waiting_human → approve → completed。

    返回 (checkpoint manager, stage4_run_id)；调用方负责 close manager。
    """
    manager, stage4_run_id, _ = await _run_stage4_to_completed(env, monkeypatch, connection_uri)
    sessionmaker = env["sessionmaker"]
    try:
        execution = _make_execution(sessionmaker, manager)
        try:
            coordinator = ResearchExecutionRecoveryCoordinator(sessionmaker, execution)
            assert await coordinator.recover_interrupted_chains() == 1
            stage5 = await _wait_for_stage5_waiting_human(sessionmaker, env["task_id"])
            await execution.resume_human(UUID(stage5["run_id"]), "approve", "审核通过")
            assert await _get_run_status(sessionmaker, UUID(stage5["run_id"])) == "completed"
        finally:
            await execution.close()
    except BaseException:
        await manager.close()
        raise
    return manager, stage4_run_id


async def _derive_expected_evidence(
    env,
    execution: ResearchExecutionService,
    stage4_run_id: UUID,
) -> tuple[set[UUID], set[UUID]]:
    """独立推导期望 evidence 集，返回 (work_item 输入集, 完整期望集)。

    完整期望集 = work item 输入证据 ∪ verified claims 的 evidence_card_ids（与
    TaskArtifactService 相同的 checkpoint 恢复路径，但测试侧独立组装）。只断言
    「期望集中实际存在于 evidence_cards 的行」。
    """
    runner = execution.stage4_runner_factory()
    state = await runner.read_checkpoint_state(stage4_run_id)
    work_item_ids: set[UUID] = set()
    for item in state.get("analysis_work_items") or []:
        for key in _WORK_ITEM_EVIDENCE_KEYS:
            for value in item.get(key) or []:
                work_item_ids.add(value if isinstance(value, UUID) else UUID(value))
    all_ids = set(work_item_ids)
    synthesis_id = state.get("synthesis_id")
    if synthesis_id:
        sessionmaker = env["sessionmaker"]
        service = SynthesisService(sessionmaker)
        async with sessionmaker() as session:
            verified = await service.verify_synthesis_integrity(session, UUID(synthesis_id))
        for claim in verified.verified_claims:
            all_ids.update(claim.evidence_card_ids)
    return work_item_ids, all_ids


def _stage5_request(env, synthesis_result_id: UUID) -> Stage5WorkflowRequest:
    return Stage5WorkflowRequest(
        task_id=env["task_id"],
        company_id=env["company_id"],
        research_question=_QUESTION,
        analysis_as_of=_AS_OF,
        synthesis_result_id=synthesis_result_id,
    )


# ---------------------------------------------------------------- full chain


async def test_full_chain_artifacts(env, monkeypatch, connection_uri) -> None:
    manager, stage4_run_id = await _run_full_chain_to_completed(env, monkeypatch, connection_uri)
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]
    company_id = env["company_id"]
    try:
        execution = _make_execution(sessionmaker, manager)
        artifact = _make_artifact_service(sessionmaker, manager)

        # ---- analysis：work items + claims + synthesis（checkpoint 权威） ----
        analysis = await artifact.get_analysis(task_id)
        assert isinstance(analysis, AnalysisArtifactResponse)
        assert analysis.company_id == company_id
        assert analysis.research_question == _QUESTION
        assert analysis.analysis_as_of == _AS_OF
        assert [w.analysis_type for w in analysis.work_items] == [
            "business",
            "risk",
            "financial",
            "macro",
            "valuation",
        ]
        assert all(w.item_id for w in analysis.work_items)
        assert all(w.claim_ids for w in analysis.work_items), "每个 work item 应有产物 claim"
        assert analysis.synthesis_id is not None
        assert analysis.synthesis_fingerprint
        assert analysis.work_items_available is True
        assert len(analysis.claims) >= 2  # synthesis 合法下界
        assert all(c.claim_id for c in analysis.claims)
        # 章节 H：themes / conflicts / evidence_gaps 按真实 synthesis v1 投影。
        assert len(analysis.themes) == 1
        assert analysis.themes[0].claim_ids
        assert analysis.evidence_gaps
        for theme in analysis.themes:
            assert theme.claim_ids, "alias refs 应解析为真实 claim IDs"
        for gap in analysis.evidence_gaps:
            assert gap.claim_ids

        # ---- evidence：任务级精确集 + 分页信封 + 关系（章节 G） ----
        work_item_input_ids, expected_evidence = await _derive_expected_evidence(
            env, execution, stage4_run_id
        )
        assert work_item_input_ids, "checkpoint 应恢复非空 work item 输入 evidence"
        async with sessionmaker() as session:
            existing_rows, existing_total = await EvidenceCardRepository(session).list_by_ids(
                sorted(expected_evidence, key=str), limit=len(expected_evidence), offset=0
            )
        existing_ids = {row.evidence_card_id for row in existing_rows}
        assert work_item_input_ids <= existing_ids, "work item 输入卡必须真实存在"
        assert existing_ids, "期望集应含实际存在的 evidence 卡"

        evidence = await artifact.get_evidence(task_id, limit=100, offset=0)
        assert isinstance(evidence, EvidenceArtifactListResponse)
        assert evidence.total == existing_total
        assert {e.evidence_card_id for e in evidence.items} == existing_ids
        allowed_company_ids = {company_id, *env["peer_company_ids"]}
        assert all(
            e.company_id is None or e.company_id in allowed_company_ids for e in evidence.items
        )
        assert all(e.evidence_statement for e in evidence.items)
        # 章节 G：used_by_claim_ids（必填）+ claim_relations（推荐）一致。
        linked = [e for e in evidence.items if e.used_by_claim_ids]
        assert linked, "至少部分卡应被 canonical claims 引用"
        for e in linked:
            assert e.claim_relations
            assert {r.claim_id for r in e.claim_relations} == set(e.used_by_claim_ids)
            assert {r.relation for r in e.claim_relations} <= {
                "supports",
                "contradicts",
                "context",
            }

        # 分页切分正确（limit/offset 语义）。
        first_page = await artifact.get_evidence(task_id, limit=2, offset=0)
        assert first_page.total == evidence.total
        assert len(first_page.items) == 2
        assert first_page.items[0].evidence_card_id == evidence.items[0].evidence_card_id

        # ---- sources：dual-origin——document（反查 source_id）+ macro（observation 闭包） ----
        expected_doc_sources = {row.source_id for row in existing_rows if row.source_id is not None}
        sources = await artifact.get_sources(task_id, limit=100, offset=0)
        assert isinstance(sources, SourceArtifactListResponse)
        doc_sources = {s.source_id for s in sources.items if s.source_id is not None}
        macro_sources = [s for s in sources.items if s.origin_type == "macro_observation"]
        assert doc_sources == expected_doc_sources
        assert macro_sources, "sources 必须含 macro source 投影（章节 L）"
        for s in macro_sources:
            assert s.source_id is None
            assert s.source_type == "macro_series"
            assert s.source_identity and s.title and s.label and s.locator_summary
        assert sources.total == len(doc_sources) + len(macro_sources)
        assert all(s.title for s in sources.items)
        assert all(
            s.company_id is None or s.company_id in allowed_company_ids for s in sources.items
        )

        # ---- report：verify_report_integrity read-side 投影（章节 I） ----
        report = await artifact.get_report(task_id)
        assert isinstance(report, ReportArtifactResponse)
        assert report.report_id is not None
        assert report.outline_id is not None
        assert report.company_id == company_id
        assert report.report_fingerprint
        assert report.analysis_as_of == _AS_OF
        assert report.section_count and report.section_count > 0
        assert report.sections
        for section in report.sections:
            assert section.section_id and section.title
            assert section.section_order >= 1
            for paragraph in section.paragraphs:
                assert paragraph.text
                assert paragraph.paragraph_index >= 0

        # ---- reviews：audit + issues + 分层（章节 J） ----
        reviews = await artifact.get_reviews(task_id)
        assert isinstance(reviews, ReviewsArtifactResponse)
        assert reviews.audit_id is not None
        assert reviews.report_id == report.report_id
        assert reviews.audit_status == "fail"
        assert reviews.recommended_route == "human_review"
        assert reviews.issue_count == 1
        assert len(reviews.issues) == 1
        issue = reviews.issues[0]
        assert issue.ordinal == 1
        assert issue.severity == "critical"
        assert issue.issue_type == "unresolved_conflict"
        assert issue.section_id
        assert issue.message
        # 分层：check + review_action 存在；human_review / research_backflow 按状态。
        assert reviews.check is not None
        assert reviews.check.check_result_id is not None
        assert reviews.review_action is not None
        assert reviews.review_action.action_type == "human_review"
        assert reviews.human_review is not None
        assert reviews.human_review.decision == "approve"
        assert reviews.human_review.comment == "审核通过"
        assert reviews.human_review.comment_exists is True
        assert reviews.research_backflow is None

        # ---- count_artifacts：与各分项一致（任务级计数） ----
        summary = await artifact.count_artifacts(task_id)
        assert summary.source_count == sources.total
        assert summary.evidence_count == evidence.total
        assert summary.claim_count == len(analysis.claims)
        assert summary.report_count == 1
        assert summary.review_issue_count == reviews.issue_count
    finally:
        await manager.close()


# ---------------------------------------------------------------- chapter K：backflow lineage


async def test_research_backflow_lineage_anchors_new_synthesis(
    env, monkeypatch, connection_uri
) -> None:
    """章节 K：S1 → Stage5(research) → 新综合 S2（无匹配 Stage4）→ continuation run2 finalize。

    workspace 锚定 S2 / R2 / run2 的 Audit；analysis 不混 S1；work_items_available=false；
    evidence 不混 S1-only 卡。
    """
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]

    # ---- S1：本任务 Stage4 run A → synthesis_result S1 ----
    ids = await _seed_worker_inputs(env, monkeypatch)
    request_a = _stage4_request(env, ids)
    s1_result_id = await _run_stage4_graph(env, connection_uri, request_a, _good_models())

    # ---- Stage5 run B：audit=research → 创建 research request ----
    deps = _stage5_deps(
        sessionmaker,
        draft_model=FakeDraftSectionModel(decision_factory=valid_decision_for),
        audit_model=FakeAuditModel(decision_factory=research_decision),
        revision_model=FakeRevisionWriterModel(),
    )
    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        runner = Stage5WorkflowRunner(sessionmaker, manager, deps)
        req_b = _stage5_request(env, s1_result_id)
        run_b = await runner.create_stage5_run(req_b)
        await runner.execute_stage5(run_b.run_id, req_b)
    finally:
        await manager.close()
    backflow = deps.research_backflow_service
    research_request = await backflow.create_or_get_request(run_b.run_id)

    # ---- S2：第二任务上跑 Stage4（同公司/问题/cutoff，biz 卡换新）→ 本任务无匹配 Stage4 ----
    task2_id = await _seed_research_task(sessionmaker)
    extra = await _seed_claim_doc_card(
        env,
        statement="2024年公司经营现金流净额同比增长20%。",
        source_url="https://www.xinhuanet.com/2026/0809/s2cash.htm",
    )
    items = []
    for item in request_a.analysis_work_items:
        if item.item_id == "biz":
            items.append(item.model_copy(update={"evidence_card_ids": [extra["evidence_card_id"]]}))
        else:
            items.append(item)
    request_b = Stage4WorkflowRequest(
        task_id=task2_id,
        company_id=env["company_id"],
        research_question=_QUESTION,
        analysis_as_of=_AS_OF,
        analysis_work_items=items,
    )
    env2 = {**env, "task_id": task2_id}
    s2_result_id = await _run_stage4_graph(env2, connection_uri, request_b, _two_theme_models())
    assert s2_result_id != s1_result_id

    # ---- fulfill → continuation（回到本任务）→ Stage5 run C finalize ----
    fulfillment = await backflow.fulfill_request(research_request.research_request_id, s2_result_id)
    assert fulfillment.replayed is False
    cont = await backflow.build_stage5_continuation_request(fulfillment.fulfillment_id)
    assert cont.task_id == task_id
    assert cont.synthesis_result_id == s2_result_id
    deps2 = _stage5_deps(
        sessionmaker,
        draft_model=FakeDraftSectionModel(decision_factory=valid_decision_for),
        audit_model=FakeAuditModel(decision_factory=pass_decision),
        revision_model=FakeRevisionWriterModel(),
    )
    manager2 = LangGraphCheckpointManager(connection_uri)
    await manager2.setup()
    try:
        runner2 = Stage5WorkflowRunner(sessionmaker, manager2, deps2)
        run_c = await runner2.create_stage5_run(cont)
        await runner2.execute_stage5(run_c.run_id, cont)
    finally:
        await manager2.close()
    assert run_c.run_id != run_b.run_id

    # ---- workspace 锚定 S2 / R2 / run2 Audit；不混 S1 ----
    artifact = _make_artifact_service(sessionmaker, manager2)
    analysis = await artifact.get_analysis(task_id)
    assert analysis.synthesis_result_id == s2_result_id
    # S2 是 _two_theme_models（2 theme + 1 conflict）——若混入 S1（1 theme）则断言失败。
    assert len(analysis.themes) == 2
    assert len(analysis.conflicts) == 1
    assert analysis.evidence_gaps
    assert analysis.work_items_available is False
    assert analysis.work_items == []
    assert analysis.claims, "S2 的 verified claims 应非空"

    evidence = await artifact.get_evidence(task_id, limit=100, offset=0)
    evidence_ids = {e.evidence_card_id for e in evidence.items}
    assert extra["evidence_card_id"] in evidence_ids, "S2 证据必须含新 biz 卡"
    # 证据集必须 == S2 claims 的 evidence 闭包（work_items_available=false → 不注入
    # 任何 S1 work-item 输入）。注意：S2 的 financial claim 复用共享 calc（source
    # 卡 = S1 biz 卡），因此 biz 卡经 calc 合法出现在 S2 闭包中——不是混入 S1
    # work item，而是 S2 自身 financial claim 的证据闭包。
    s2_evidence_closure = {card for claim in analysis.claims for card in claim.evidence_card_ids}
    assert evidence_ids == s2_evidence_closure, "证据必须恰好是 S2 claims 的 evidence 闭包"

    report = await artifact.get_report(task_id)
    assert report.report_id is not None
    # R2 = 最新报告（run2 产物），不是 run1 的 R1。
    async with sessionmaker() as session:
        latest_report = (
            await session.execute(
                text(
                    "SELECT report_id FROM reports ORDER BY created_at DESC, report_id DESC LIMIT 1"
                )
            )
        ).scalar_one()
    assert report.report_id == latest_report

    reviews = await artifact.get_reviews(task_id)
    assert reviews.report_id == report.report_id
    assert reviews.audit_status == "pass"
    assert reviews.recommended_route == "pass"
    assert reviews.research_backflow is not None
    assert reviews.research_backflow.fulfilled is True
    assert reviews.research_backflow.new_synthesis_result_id == s2_result_id
    assert reviews.human_review is None

    # 报告正文引用的 claim 全部来自 S2 的 claims（不混 S1-only claim）。
    analysis_claims = {c.claim_id for c in analysis.claims}
    report_claims = {
        claim_id
        for section in report.sections
        for paragraph in section.paragraphs
        for claim_id in paragraph.claim_ids
    }
    assert report_claims, "R2 正文应引用 claims"
    assert report_claims <= analysis_claims

    # count_artifacts 与各 tab 一致。
    summary = await artifact.count_artifacts(task_id)
    assert summary.report_count == 1
    assert summary.evidence_count == evidence.total
    sources = await artifact.get_sources(task_id, limit=100, offset=0)
    assert summary.source_count == sources.total


# ----------------------------------------- Gate0-C/D：backflow 反查 task-scoped + verify


async def _seed_backflow_chain(
    env,
    monkeypatch,
    connection_uri,
    *,
    second_fulfillment: bool = False,
    with_continuation: bool = True,
) -> dict:
    """S1(task1) → S2(task2) → Stage5 run B(research) → request → fulfill(s2) →
    可选 continuation run C(finalize)。

    `second_fulfillment=True` 时额外创建第二个 research run B2 → request → 也用
    s2 完成 fulfillment（同 task 内两条 fulfillment 共享 new_synthesis_result_id，
    供 Gate0-C 歧义测试）。`with_continuation=False` 时不做 run C，run B 即 latest
    Stage5 run（checkpoint 携带 research_request_id → request_id 路径，供 Gate0-D）。
    返回句柄；调用方负责 `await handle["manager_artifact"].close()`。
    """
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]

    # ---- S1（task1）----
    ids = await _seed_worker_inputs(env, monkeypatch)
    request_a = _stage4_request(env, ids)
    s1_result_id = await _run_stage4_graph(env, connection_uri, request_a, _good_models())

    # ---- S2（task2，同 company/question/cutoff，biz 卡换新）→ 本任务无匹配 Stage4 ----
    task2_id = await _seed_research_task(sessionmaker)
    extra = await _seed_claim_doc_card(
        env,
        statement="2024年公司经营现金流净额同比增长20%。",
        source_url="https://www.xinhuanet.com/2026/0809/s2cash.htm",
    )
    items = []
    for item in request_a.analysis_work_items:
        if item.item_id == "biz":
            items.append(item.model_copy(update={"evidence_card_ids": [extra["evidence_card_id"]]}))
        else:
            items.append(item)
    request_s2 = Stage4WorkflowRequest(
        task_id=task2_id,
        company_id=env["company_id"],
        research_question=_QUESTION,
        analysis_as_of=_AS_OF,
        analysis_work_items=items,
    )
    env2 = {**env, "task_id": task2_id}
    s2_result_id = await _run_stage4_graph(env2, connection_uri, request_s2, _two_theme_models())

    def _research_runner(manager):
        deps = _stage5_deps(
            sessionmaker,
            draft_model=FakeDraftSectionModel(decision_factory=valid_decision_for),
            audit_model=FakeAuditModel(decision_factory=research_decision),
            revision_model=FakeRevisionWriterModel(),
        )
        return Stage5WorkflowRunner(sessionmaker, manager, deps), deps

    async def _run_research_stage5(manager) -> tuple[UUID, object]:
        runner, deps = _research_runner(manager)
        run = await runner.create_stage5_run(_stage5_request(env, s1_result_id))
        await runner.execute_stage5(run.run_id, _stage5_request(env, s1_result_id))
        return run.run_id, deps

    # ---- run B（research 路由）：execute 时 create_research_backflow_request 节点
    #      已写 research_request_id 到 checkpoint + 创建 request B ----
    manager_b = LangGraphCheckpointManager(connection_uri)
    await manager_b.setup()
    try:
        run_b_id, deps_b = await _run_research_stage5(manager_b)
    finally:
        await manager_b.close()
    backflow_b = deps_b.research_backflow_service
    request_b = await backflow_b.create_or_get_request(run_b_id)  # replay
    fulfillment_b = await backflow_b.fulfill_request(request_b.research_request_id, s2_result_id)
    assert fulfillment_b.replayed is False

    second_fulfillment_id = None
    if second_fulfillment:
        manager_b2 = LangGraphCheckpointManager(connection_uri)
        await manager_b2.setup()
        try:
            run_b2_id, deps_b2 = await _run_research_stage5(manager_b2)
        finally:
            await manager_b2.close()
        backflow_b2 = deps_b2.research_backflow_service
        request_b2 = await backflow_b2.create_or_get_request(run_b2_id)
        fulfillment_b2 = await backflow_b2.fulfill_request(
            request_b2.research_request_id, s2_result_id
        )
        assert fulfillment_b2.replayed is False
        second_fulfillment_id = fulfillment_b2.fulfillment_id

    run_c_id = None
    if with_continuation:
        cont = await backflow_b.build_stage5_continuation_request(fulfillment_b.fulfillment_id)
        assert cont.task_id == task_id
        assert cont.synthesis_result_id == s2_result_id
        deps_c = _stage5_deps(
            sessionmaker,
            draft_model=FakeDraftSectionModel(decision_factory=valid_decision_for),
            audit_model=FakeAuditModel(decision_factory=pass_decision),
            revision_model=FakeRevisionWriterModel(),
        )
        manager_c = LangGraphCheckpointManager(connection_uri)
        await manager_c.setup()
        runner_c = Stage5WorkflowRunner(sessionmaker, manager_c, deps_c)
        run_c = await runner_c.create_stage5_run(cont)
        try:
            await runner_c.execute_stage5(run_c.run_id, cont)
        except BaseException:
            await manager_c.close()
            raise
        run_c_id = run_c.run_id

    manager_artifact = LangGraphCheckpointManager(connection_uri)
    await manager_artifact.setup()
    artifact = _make_artifact_service(sessionmaker, manager_artifact)
    return {
        "sessionmaker": sessionmaker,
        "task_id": task_id,
        "task2_id": task2_id,
        "env2": env2,
        "s1_result_id": s1_result_id,
        "s2_result_id": s2_result_id,
        "request_b_id": request_b.research_request_id,
        "fulfillment_b_id": fulfillment_b.fulfillment_id,
        "second_fulfillment_id": second_fulfillment_id,
        "run_c_id": run_c_id,
        "manager_artifact": manager_artifact,
        "artifact": artifact,
    }


async def test_backflow_reverse_lookup_task_scoped(env, monkeypatch, connection_uri) -> None:
    """Gate0-C：canonical-synthesis 反查必须 task-scoped。

    task1（有 fulfillment）→ fulfilled 投影；task2 用同一 S2 跑 finalize Stage5
    （canonical=s2）但无 fulfillment → 反查 0 行 → None，绝不跨任务命中。
    """
    handle = await _seed_backflow_chain(env, monkeypatch, connection_uri)
    sessionmaker = handle["sessionmaker"]
    task1_id = handle["task_id"]
    task2_id = handle["task2_id"]
    env2 = handle["env2"]
    try:
        reviews1 = await handle["artifact"].get_reviews(task1_id)
        assert reviews1.research_backflow is not None
        assert reviews1.research_backflow.fulfilled is True
        assert reviews1.research_backflow.new_synthesis_result_id == handle["s2_result_id"]

        deps2 = _stage5_deps(
            sessionmaker,
            draft_model=FakeDraftSectionModel(decision_factory=valid_decision_for),
            audit_model=FakeAuditModel(decision_factory=pass_decision),
            revision_model=FakeRevisionWriterModel(),
        )
        manager2 = LangGraphCheckpointManager(connection_uri)
        await manager2.setup()
        artifact2 = _make_artifact_service(sessionmaker, manager2)
        try:
            runner2 = Stage5WorkflowRunner(sessionmaker, manager2, deps2)
            run2 = await runner2.create_stage5_run(_stage5_request(env2, handle["s2_result_id"]))
            await runner2.execute_stage5(run2.run_id, _stage5_request(env2, handle["s2_result_id"]))
            reviews2 = await artifact2.get_reviews(task2_id)
            assert reviews2.research_backflow is None
        finally:
            await manager2.close()
    finally:
        await handle["manager_artifact"].close()


async def test_backflow_reverse_lookup_ambiguous_fulfillment_fails(
    env, monkeypatch, connection_uri
) -> None:
    """Gate0-C：同 task 内多条 fulfillment 共享同一 new_synthesis_result_id →
    TaskArtifactIntegrityError（绝不静默选一行）。"""
    handle = await _seed_backflow_chain(env, monkeypatch, connection_uri, second_fulfillment=True)
    try:
        with pytest.raises(TaskArtifactIntegrityError):
            await handle["artifact"].get_reviews(handle["task_id"])
    finally:
        await handle["manager_artifact"].close()


async def test_backflow_fulfillment_tamper_yields_integrity_error(
    env, monkeypatch, connection_uri
) -> None:
    """Gate0-D：canonical 反查路径必须 verify fulfillment——SQL tamper fulfillment
    fingerprint → get_reviews → TaskArtifactIntegrityError（不 repair）。"""
    handle = await _seed_backflow_chain(env, monkeypatch, connection_uri)
    sessionmaker = handle["sessionmaker"]
    try:
        assert (
            await handle["artifact"].get_reviews(handle["task_id"])
        ).research_backflow is not None
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "UPDATE research_backflow_fulfillments SET fulfillment_fingerprint = "
                    "repeat('0', 64) WHERE fulfillment_id = :fid"
                ).bindparams(fid=handle["fulfillment_b_id"])
            )
            await session.commit()
        with pytest.raises(TaskArtifactIntegrityError):
            await handle["artifact"].get_reviews(handle["task_id"])
    finally:
        await handle["manager_artifact"].close()


async def test_backflow_request_id_path_fulfillment_verify(
    env, monkeypatch, connection_uri
) -> None:
    """Gate0-D：checkpoint 携带 research_request_id 的路径也必须 verify
    fulfillment（不再直接 repo 读 → 投影）——tamper → get_reviews →
    TaskArtifactIntegrityError。"""
    handle = await _seed_backflow_chain(env, monkeypatch, connection_uri, with_continuation=False)
    sessionmaker = handle["sessionmaker"]
    try:
        reviews = await handle["artifact"].get_reviews(handle["task_id"])
        assert reviews.research_backflow is not None
        assert reviews.research_backflow.fulfilled is True
        assert reviews.research_backflow.new_synthesis_result_id == handle["s2_result_id"]
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "UPDATE research_backflow_fulfillments SET fulfillment_fingerprint = "
                    "repeat('0', 64) WHERE fulfillment_id = :fid"
                ).bindparams(fid=handle["fulfillment_b_id"])
            )
            await session.commit()
        with pytest.raises(TaskArtifactIntegrityError):
            await handle["artifact"].get_reviews(handle["task_id"])
    finally:
        await handle["manager_artifact"].close()


# ---------------------------------------------------------------- chapter L：macro evidence/source


async def test_macro_evidence_sources_included(env, monkeypatch, connection_uri) -> None:
    """章节 L：canonical synthesis 引用 ≥1 条 macro_observation 证据；sources/evidence
    投影含 macro source（source_id=NULL 不丢失）。"""
    sessionmaker = env["sessionmaker"]
    request = _stage4_request(env, await _seed_worker_inputs(env, monkeypatch))
    s_result_id = await _run_stage4_graph(env, connection_uri, request, _good_models())
    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        artifact = _make_artifact_service(sessionmaker, manager)
        analysis = await artifact.get_analysis(env["task_id"])
        assert analysis.synthesis_result_id == s_result_id

        evidence = await artifact.get_evidence(env["task_id"], limit=100, offset=0)
        assert evidence.total == len(evidence.items), "小数据集应单页覆盖"
        macro_cards = [e for e in evidence.items if e.origin_type == "macro_observation"]
        assert macro_cards, "evidence 必须含 macro_observation 卡"
        for card in macro_cards:
            assert card.source_id is None
            assert card.macro_observation_id is not None
            assert card.macro_snapshot_id is not None
            assert card.macro_series_id is not None

        sources = await artifact.get_sources(env["task_id"], limit=100, offset=0)
        macro_sources = [s for s in sources.items if s.origin_type == "macro_observation"]
        assert macro_sources, "sources 必须含 macro source 投影"
        for s in macro_sources:
            assert s.source_id is None
            assert s.source_type == "macro_series"
            assert s.source_identity and s.title and s.label and s.locator_summary
            assert s.fetched_at is not None
            assert s.authority_tier is not None

        # 独立推导 macro series 数：obs → snapshot → series，与投影去重口径一致。
        async with sessionmaker() as session:
            obs_rows = (
                await session.execute(
                    select(MacroObservationModel.snapshot_id).where(
                        MacroObservationModel.observation_id.in_(
                            [c.macro_observation_id for c in macro_cards]
                        )
                    )
                )
            ).all()
            snap_ids = {sid for (sid,) in obs_rows if sid is not None}
            series_rows = (
                await session.execute(
                    select(MacroDatasetSnapshotModel.series_id).where(
                        MacroDatasetSnapshotModel.snapshot_id.in_(list(snap_ids))
                    )
                )
            ).all()
        expected_series = {sid for (sid,) in series_rows if sid is not None}
        assert expected_series
        assert len(macro_sources) == len(expected_series)

        # 每个 macro evidence 卡都有对应 source 投影（按 identity 存在性）。
        summary = await artifact.count_artifacts(env["task_id"])
        assert summary.source_count == sources.total
    finally:
        await manager.close()


# ---------------------------------------------------------------- chapter M：same-company isolation


async def test_same_company_task_isolation(env, monkeypatch, connection_uri) -> None:
    """章节 M：同一公司两个任务，各自只看到自己的 evidence / analysis（任务级 scoped）。"""
    sessionmaker = env["sessionmaker"]
    task_a = env["task_id"]

    ids = await _seed_worker_inputs(env, monkeypatch)
    request_a = _stage4_request(env, ids)
    s_a_result = await _run_stage4_graph(env, connection_uri, request_a, _good_models())

    task_b = await _seed_research_task(sessionmaker)
    env_b = {**env, "task_id": task_b}
    extra_b = await _seed_claim_doc_card(
        env,
        statement="另一任务独有的经营现金流转好主张。",
        source_url="https://www.sse.com.cn/2026/0809/taskb-cash.htm",
    )
    items = []
    for item in request_a.analysis_work_items:
        if item.item_id == "biz":
            items.append(
                item.model_copy(update={"evidence_card_ids": [extra_b["evidence_card_id"]]})
            )
        else:
            items.append(item)
    request_b = Stage4WorkflowRequest(
        task_id=task_b,
        company_id=env["company_id"],
        research_question=_QUESTION,
        analysis_as_of=_AS_OF,
        analysis_work_items=items,
    )
    s_b_result = await _run_stage4_graph(env_b, connection_uri, request_b, _good_models())

    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        artifact = _make_artifact_service(sessionmaker, manager)
        ev_a = await artifact.get_evidence(task_a, limit=100, offset=0)
        ev_b = await artifact.get_evidence(task_b, limit=100, offset=0)
        ids_a = {e.evidence_card_id for e in ev_a.items}
        ids_b = {e.evidence_card_id for e in ev_b.items}
        # 各自独有输入卡：A 有 biz_A，B 有 biz_B。
        assert ids["biz_card"] in ids_a
        assert extra_b["evidence_card_id"] in ids_b
        # 任务级隔离核心证明：task B 独有卡**不泄漏**进 task A（若按 company 全集
        # 查询，extra_b 必然出现在 A 的 evidence 里）。
        assert extra_b["evidence_card_id"] not in ids_a
        # 共享输入卡（risk 等）两者都含（同一公司，正常共享来源）。
        assert ids["risk_card"] in ids_a and ids["risk_card"] in ids_b
        # 证据集按任务区分，非 company 全集同一份。
        assert ids_a != ids_b

        an_a = await artifact.get_analysis(task_a)
        an_b = await artifact.get_analysis(task_b)
        assert an_a.synthesis_result_id == s_a_result
        assert an_b.synthesis_result_id == s_b_result
        assert an_a.synthesis_result_id != an_b.synthesis_result_id
        # work-item 级隔离：各任务的 biz work item 引用自己的输入卡（checkpoint
        # 权威），互不混用。
        biz_item_a = next(w for w in an_a.work_items if w.item_id == "biz")
        biz_item_b = next(w for w in an_b.work_items if w.item_id == "biz")
        assert biz_item_a.evidence_card_ids == [ids["biz_card"]]
        assert biz_item_b.evidence_card_ids == [extra_b["evidence_card_id"]]
        # 注：task B 的 evidence 仍会经共享 financial calc（source=biz_A）引用
        # biz_A——这是 S_B financial claim 的合法证据闭包，非服务层任务泄漏。

        # 计数任务级：不按 company 全集。
        s_a = await artifact.count_artifacts(task_a)
        s_b = await artifact.count_artifacts(task_b)
        assert s_a.evidence_count == len(ids_a)
        assert s_b.evidence_count == len(ids_b)
        assert s_a.claim_count == len(an_a.claims)
        assert s_b.claim_count == len(an_b.claims)
    finally:
        await manager.close()


# ---------------------------------------------------------------- chapter D：integrity error


async def test_integrity_error_when_artifact_corrupt(env, monkeypatch, connection_uri) -> None:
    """章节 D：checkpoint 引用的 report 产物损坏（删除 draft_section）→ 409
    TaskArtifactIntegrityError；不 repair / 不降级为空；无关 tab 仍可读。"""
    sessionmaker = env["sessionmaker"]
    manager, _ = await _run_full_chain_to_completed(env, monkeypatch, connection_uri)
    try:
        artifact = _make_artifact_service(sessionmaker, manager)
        # 损坏：删除一条 draft_section → verify_report_integrity / verify_audit_integrity 无法重建。
        async with sessionmaker() as session:
            draft_id = (
                await session.execute(text("SELECT draft_section_id FROM draft_sections LIMIT 1"))
            ).scalar_one()
            await session.execute(
                text("DELETE FROM draft_sections WHERE draft_section_id = :id").bindparams(
                    id=draft_id
                )
            )
            await session.commit()

        with pytest.raises(TaskArtifactIntegrityError):
            await artifact.get_report(env["task_id"])
        with pytest.raises(TaskArtifactIntegrityError):
            await artifact.get_reviews(env["task_id"])
        with pytest.raises(TaskArtifactIntegrityError):
            await artifact.count_artifacts(env["task_id"])
        # 完整性失败不降级为空：分析 tab（不依赖 report/audit）仍可读。
        analysis = await artifact.get_analysis(env["task_id"])
        assert analysis.synthesis_result_id is not None
    finally:
        await manager.close()


# ---------------------------------------------------------------- chapter E：no-LLM read


async def test_read_only_no_api_key(env, monkeypatch, connection_uri) -> None:
    """章节 E：只读路径 0 LLM——移除 DEEPSEEK_API_KEY 后生产 DI 构建 + 全部读取成功。"""
    sessionmaker = env["sessionmaker"]
    manager, _ = await _run_full_chain_to_completed(env, monkeypatch, connection_uri)
    try:
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        get_settings.cache_clear()
        deps = create_stage5_dependencies(get_settings(), sessionmaker)
        artifact = TaskArtifactService.from_dependencies(sessionmaker, manager, deps)

        sources = await artifact.get_sources(env["task_id"], limit=100, offset=0)
        assert sources.total > 0
        evidence = await artifact.get_evidence(env["task_id"], limit=100, offset=0)
        assert evidence.total > 0
        analysis = await artifact.get_analysis(env["task_id"])
        assert analysis.synthesis_result_id is not None
        report = await artifact.get_report(env["task_id"])
        assert report.report_id is not None
        reviews = await artifact.get_reviews(env["task_id"])
        assert reviews.audit_id is not None
        summary = await artifact.count_artifacts(env["task_id"])
        assert summary.report_count == 1
    finally:
        await manager.close()


# ---------------------------------------------------------------- task not found


async def test_task_not_found(env, connection_uri) -> None:
    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        artifact = _make_artifact_service(env["sessionmaker"], manager)
        missing = uuid4()
        with pytest.raises(TaskNotFound):
            await artifact.get_evidence(missing, limit=20, offset=0)
        with pytest.raises(TaskNotFound):
            await artifact.get_sources(missing, limit=20, offset=0)
        with pytest.raises(TaskNotFound):
            await artifact.get_analysis(missing)
        with pytest.raises(TaskNotFound):
            await artifact.get_report(missing)
        with pytest.raises(TaskNotFound):
            await artifact.get_reviews(missing)
        with pytest.raises(TaskNotFound):
            await artifact.count_artifacts(missing)
    finally:
        await manager.close()


# ---------------------------------------------------------------- no run (empty)


async def test_no_run_task_returns_empty(env, connection_uri) -> None:
    new_task_id = await _seed_research_task(env["sessionmaker"])
    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        artifact = _make_artifact_service(env["sessionmaker"], manager)

        sources = await artifact.get_sources(new_task_id, limit=20, offset=0)
        assert sources.items == [] and sources.total == 0

        evidence = await artifact.get_evidence(new_task_id, limit=20, offset=0)
        assert evidence.items == [] and evidence.total == 0

        analysis = await artifact.get_analysis(new_task_id)
        assert analysis.company_id is None
        assert analysis.work_items == [] and analysis.claims == []
        assert analysis.synthesis_id is None
        assert analysis.synthesis_result_id is None

        report = await artifact.get_report(new_task_id)
        assert report.report_id is None and report.section_count is None

        reviews = await artifact.get_reviews(new_task_id)
        assert reviews.audit_id is None and reviews.issue_count == 0

        summary = await artifact.count_artifacts(new_task_id)
        assert summary.source_count == 0
        assert summary.evidence_count == 0
        assert summary.claim_count == 0
        assert summary.report_count == 0
        assert summary.review_issue_count == 0
    finally:
        await manager.close()


async def test_waiting_manual_task_shows_company_evidence(env, connection_uri) -> None:
    """P9：任务无 Stage4/5 checkpoint（waiting_manual）时，sources/evidence
    fallback 到公司已获取资料（真实 provenance，进度展示），不再显示空工作台。
    """
    import hashlib
    from datetime import UTC, datetime

    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]
    company_id = env["company_id"]
    # 清理本测试可能残留的行（共享 _cleanup 不删 research_plans/source 链）。
    async with sessionmaker() as session:
        await session.execute(
            text("DELETE FROM evidence_cards WHERE company_id = :c"), {"c": company_id}
        )
        await session.execute(
            text(
                "DELETE FROM parsed_sources WHERE source_id IN "
                "(SELECT source_id FROM source_records WHERE company_id = :c)"
            ),
            {"c": company_id},
        )
        await session.execute(
            text("DELETE FROM source_records WHERE company_id = :c"), {"c": company_id}
        )
        await session.execute(
            text(
                "DELETE FROM raw_artifacts WHERE artifact_id NOT IN "
                "(SELECT artifact_id FROM source_records)"
            )
        )
        await session.execute(
            text("DELETE FROM research_plans WHERE company_id = :c"), {"c": company_id}
        )
        await session.commit()
    plan_id = uuid4()
    raw_id = uuid4()
    source_id = uuid4()
    parsed_id = uuid4()
    card_id = uuid4()
    quote_text = "营业收入（千元） 362,012,554 400,917,045"
    fingerprint = hashlib.sha256(b"pv-fallback-test").hexdigest()
    quote_sha = hashlib.sha256(quote_text.encode()).hexdigest()
    async with sessionmaker() as session:
        await session.execute(
            text(
                "INSERT INTO research_plans (research_plan_id, task_id, company_id, "
                "plan_schema_version, planner_name, planner_version, model_id, "
                "planner_input_fingerprint, plan_fingerprint, plan_payload, "
                "planner_input_payload, planner_input_schema_version, created_at) "
                "VALUES (:pid, :tid, :cid, 2, 'pv', 1, 'test:model', :fp, :fp, "
                "'{}'::jsonb, '{}'::jsonb, 1, :now)",
            ),
            {
                "pid": plan_id,
                "tid": task_id,
                "cid": company_id,
                "fp": fingerprint,
                "now": datetime.now(UTC),
            },
        )
        await session.execute(
            text(
                "INSERT INTO raw_artifacts (artifact_id, content_sha256, storage_key, "
                "byte_size, media_type, created_at) VALUES "
                "(:id, :sha, 'k', 1, 'application/pdf', :now)",
            ),
            {"id": raw_id, "sha": fingerprint, "now": datetime.now(UTC)},
        )
        await session.execute(
            text(
                "INSERT INTO source_records (source_id, company_id, provider_key, artifact_id, "
                "document_type, title, source_url, acquisition_method, status, "
                "authority_tier_snapshot, critical_claim_eligible_snapshot, "
                "provider_capabilities_snapshot, acquired_at, created_at) "
                "VALUES (:id, :cid, 'eastmoney', :rid, 'annual_report', '测试年报', "
                "'https://example.com/a.pdf', 'automatic_discovery', 'available', 3, false, "
                "'[\"annual_report\"]'::jsonb, :now, :now)",
            ),
            {"id": source_id, "cid": company_id, "rid": raw_id, "now": datetime.now(UTC)},
        )
        await session.execute(
            text(
                "INSERT INTO parsed_sources (parsed_source_id, source_id, artifact_id, "
                "parser_name, parser_version, raw_content_sha256, parse_fingerprint, "
                "block_count, parsed_at, created_at) VALUES "
                "(:id, :sid, :rid, 'pv', 1, :sha, :fp, 1, :now, :now)",
            ),
            {
                "id": parsed_id,
                "sid": source_id,
                "rid": raw_id,
                "sha": fingerprint,
                "fp": fingerprint,
                "now": datetime.now(UTC),
            },
        )
        await session.execute(
            text(
                "INSERT INTO evidence_cards (evidence_card_id, company_id, source_id, "
                "parsed_source_id, research_question, research_question_sha256, "
                "evidence_statement, evidence_type, quote_text, quote_sha256, "
                "quote_start, quote_end, locator_refs, provider_key, "
                "authority_tier_snapshot, critical_claim_eligible_snapshot, "
                "extractor_name, extractor_version, extractor_confidence, "
                "evidence_schema_version, evidence_fingerprint, origin_type, created_at) "
                "VALUES (:id, :cid, :sid, :pid, 'q', :qsha, 's', 'metric', :qt, :qsha, 0, :qend, "
                '\'[{"type":"financial_extraction","block_id":"b"}]\'::jsonb, '
                "'eastmoney', 3, false, 'pv', 1, 'low', 1, :fp, 'financial_extraction', :now)",
            ),
            {
                "id": card_id,
                "cid": company_id,
                "sid": source_id,
                "pid": parsed_id,
                "qsha": quote_sha,
                "qt": quote_text,
                "qend": len(quote_text),
                "fp": fingerprint,
                "now": datetime.now(UTC),
            },
        )
        await session.commit()

    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        artifact = _make_artifact_service(sessionmaker, manager)
        sources = await artifact.get_sources(task_id, limit=20, offset=0)
        assert sources.total == 1
        assert sources.items[0].source_id == source_id
        evidence = await artifact.get_evidence(task_id, limit=20, offset=0)
        assert evidence.total == 1
        assert evidence.items[0].evidence_card_id == card_id
    finally:
        await manager.close()
        # 清理本测试创建的产物（共享 _cleanup 不删 research_plans/source 链）。
        async with sessionmaker() as session:
            await session.execute(
                text("DELETE FROM evidence_cards WHERE company_id = :c"), {"c": company_id}
            )
            await session.execute(
                text(
                    "DELETE FROM parsed_sources WHERE source_id IN "
                    "(SELECT source_id FROM source_records WHERE company_id = :c)"
                ),
                {"c": company_id},
            )
            await session.execute(
                text("DELETE FROM source_records WHERE company_id = :c"), {"c": company_id}
            )
            await session.execute(
                text(
                    "DELETE FROM raw_artifacts WHERE artifact_id NOT IN "
                    "(SELECT artifact_id FROM source_records)"
                )
            )
            await session.execute(
                text("DELETE FROM research_plans WHERE company_id = :c"), {"c": company_id}
            )
            await session.commit()
