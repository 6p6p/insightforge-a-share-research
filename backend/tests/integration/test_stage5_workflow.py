"""Stage5 report control workflow E2E integration tests (spec 5E.2A D/O/Q/R/S/U/V).

真实 PostgreSQL + 真实 LangGraph（AsyncPostgresSaver）+ Fake Writer / Fake
Auditor / Fake Revision Writer，全程**零真实 DeepSeek**。Stage4 → SynthesisResult
由 `_run_stage4_to_result` 真实跑出（Fake analysis models），Stage5 runner 在其上
控制 Report→Check→Audit→ReviewAction→rewrite/finalize/human/research。

覆盖（spec U/V）：
- create run：必须绑定真实 ResearchTask（缺失 → Stage5ResearchTaskNotFound）；
- finalize：Check=pass + Audit=pass → terminal finalize → run COMPLETED；
- rewrite bounded loop（spec O）：audit rewrite × 2 轮 → 超 MAX → terminal
  revision_limit_exceeded → run FAILED（2 条 revision + 3 份 Report）；
- human interrupt（spec Q）：真实 `interrupt()` → run WAITING_HUMAN；
  resume approve → finalize_on_approve（Check=pass）→ COMPLETED（durability：
  新 runner 恢复同一 thread_id）；
- human rewrite → 新 Report → 再次 human_review interrupt → resume approve →
  COMPLETED（多轮 human 裁决）；
- human cancel → terminal cancelled → run CANCELLED；
- research route（spec S）→ terminal research_required（不假装 research completed）；
- approve 安全（spec R）：Check=fail 时 approve → Stage5ApproveRequiresPassCheck
  （node 级，run FAILED）。
"""

from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.audit.contracts import ReportAuditRequest
from app.audit.service import ReportAuditService
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.domain.tasks import WorkflowEventType
from app.draft_section.errors import DraftSectionModelUnavailable
from app.draft_section.service import DraftSectionService
from app.report.check_service import ReportCheckService
from app.report.contracts import ReportAssemblyDraft
from app.report.service import ReportService
from app.report_outline.service import ReportOutlineService
from app.research_backflow.service import ResearchBackflowService
from app.review.service import ReviewActionService
from app.revision.service import RevisionService
from app.services.source_registry_service import SourceRegistryService
from app.stage5.contracts import (
    MAX_STAGE5_REVISION_ROUNDS,
    STAGE5_TERMINAL_CANCELLED,
    STAGE5_TERMINAL_FINALIZE,
    STAGE5_TERMINAL_FINALIZE_WITH_WARNINGS,
    STAGE5_TERMINAL_RESEARCH_REQUIRED,
    STAGE5_TERMINAL_REVISION_LIMIT_EXCEEDED,
    Stage5WorkflowRequest,
)
from app.stage5.dependencies import Stage5WorkflowDependencies
from app.stage5.errors import Stage5ResearchTaskNotFound
from app.stage5.nodes import make_finalize_on_approve_node
from app.stage5.runner import Stage5WorkflowRunner
from app.storage.raw_store import LocalRawArtifactStore
from app.workflows.checkpoint import LangGraphCheckpointManager
from tests.audit.fakes import FakeAuditModel, pass_decision
from tests.draft_section.fakes import FakeDraftSectionModel, valid_decision_for
from tests.integration.test_draft_section_service import (
    _AS_OF,
    _QUESTION,
    _create_outline,
    _good_models,
    _run_stage4_to_result,
    _two_theme_models,
)
from tests.integration.test_report_audit_service import (
    human_review_decision,
    research_decision,
    wording_overclaim_decision,
)
from tests.integration.test_report_check_integrity import (
    _draft_mixed_sections,
)
from tests.integration.test_revision_service import _cleanup_with_revisions
from tests.integration.test_stage4_workflow import _seed_research_task
from tests.integration.test_valuation_claim_service import _seed_company
from tests.revision.fakes import FakeRevisionWriterModel, revision_decision_for

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()


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


# ---------------------------------------------------------------- helpers


def _stage5_deps(sessionmaker, *, draft_model, audit_model, revision_model):
    """Stage5 DI：Fake models → Services（report_service 晚绑定 revision_service 断环）。"""
    draft_service = DraftSectionService(sessionmaker, draft_model)
    report_service = ReportService(sessionmaker, draft_service)
    check_service = ReportCheckService(sessionmaker, report_service)
    audit_service = ReportAuditService(sessionmaker, audit_model, check_service)
    review_service = ReviewActionService(sessionmaker, audit_service)
    revision_service = RevisionService(
        sessionmaker,
        model=revision_model,
        draft_section_service=draft_service,
        check_service=check_service,
        review_action_service=review_service,
    )
    report_service._revision_service = revision_service  # noqa: SLF001 — DI 断环
    return Stage5WorkflowDependencies(
        sessionmaker=sessionmaker,
        report_outline_service=ReportOutlineService(sessionmaker),
        draft_section_service=draft_service,
        report_service=report_service,
        report_check_service=check_service,
        report_audit_service=audit_service,
        review_action_service=review_service,
        revision_service=revision_service,
        research_backflow_service=ResearchBackflowService(
            sessionmaker, review_service, report_service
        ),
    )


async def _seed_synthesis(env, monkeypatch, connection_uri) -> UUID:
    """完整 Stage4 graph → synthesis_result_id（Fake analysis models）。"""
    return await _run_stage4_to_result(env, monkeypatch, connection_uri, _good_models())


def _request(env, synthesis_result_id: UUID) -> Stage5WorkflowRequest:
    return Stage5WorkflowRequest(
        task_id=env["task_id"],
        company_id=env["company_id"],
        research_question=_QUESTION,
        analysis_as_of=_AS_OF,
        synthesis_result_id=synthesis_result_id,
    )


async def _run_count(sessionmaker, table: str) -> int:
    async with sessionmaker() as session:
        return int((await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())


async def _run_row(sessionmaker, run_id: UUID) -> dict:
    async with sessionmaker() as session:
        return dict(
            (
                await session.execute(
                    text(
                        "SELECT status, error_code, error_message FROM workflow_runs "
                        "WHERE run_id = :rid"
                    ).bindparams(rid=run_id)
                )
            )
            .mappings()
            .one()
        )


async def _revision_count(sessionmaker) -> int:
    return await _run_count(sessionmaker, "draft_section_revisions")


# ---------------------------------------------------------------- create（spec V）


async def test_create_run_requires_real_task(env, monkeypatch, connection_uri) -> None:
    synthesis_result_id = await _seed_synthesis(env, monkeypatch, connection_uri)
    request = Stage5WorkflowRequest(
        task_id=uuid4(),  # 不存在的 task → 拒绝（不猜任务、不自动创建 fake task）
        company_id=env["company_id"],
        research_question=_QUESTION,
        analysis_as_of=_AS_OF,
        synthesis_result_id=synthesis_result_id,
    )
    deps = _stage5_deps(
        env["sessionmaker"],
        draft_model=FakeDraftSectionModel(decision_factory=valid_decision_for),
        audit_model=FakeAuditModel(decision_factory=pass_decision),
        revision_model=FakeRevisionWriterModel(),
    )
    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        runner = Stage5WorkflowRunner(env["sessionmaker"], manager, deps)
        with pytest.raises(Stage5ResearchTaskNotFound):
            await runner.create_stage5_run(request)
    finally:
        await manager.close()


# ---------------------------------------------------------------- finalize


async def test_finalize_e2e(env, monkeypatch, connection_uri) -> None:
    synthesis_result_id = await _seed_synthesis(env, monkeypatch, connection_uri)
    request = _request(env, synthesis_result_id)
    deps = _stage5_deps(
        env["sessionmaker"],
        draft_model=FakeDraftSectionModel(decision_factory=valid_decision_for),
        audit_model=FakeAuditModel(decision_factory=pass_decision),
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

    assert result["terminal"] == STAGE5_TERMINAL_FINALIZE
    assert result["report_id"] is not None
    assert result["check_result_id"] is not None
    assert result["audit_id"] is not None
    assert result["review_action_id"] is not None
    assert (await runner.get_run(run.run_id)).status.value == "completed"
    assert await _revision_count(env["sessionmaker"]) == 0
    # 事件序列包含完成事件。
    async with env["sessionmaker"]() as session:
        event_types = {
            row[0]
            for row in (
                await session.execute(
                    text("SELECT event_type FROM workflow_events WHERE run_id = :rid").bindparams(
                        rid=run.run_id
                    )
                )
            ).all()
        }
    assert WorkflowEventType.RUN_COMPLETED.value in event_types


# ---------------------------------------------------------------- rewrite bounded loop（spec O）


async def test_rewrite_loop_bounded_limit_fails(env, monkeypatch, connection_uri) -> None:
    synthesis_result_id = await _seed_synthesis(env, monkeypatch, connection_uri)
    request = _request(env, synthesis_result_id)
    revision_model = FakeRevisionWriterModel(decision_factory=revision_decision_for)
    deps = _stage5_deps(
        env["sessionmaker"],
        draft_model=FakeDraftSectionModel(decision_factory=valid_decision_for),
        audit_model=FakeAuditModel(decision_factory=wording_overclaim_decision),
        revision_model=revision_model,
    )
    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        runner = Stage5WorkflowRunner(env["sessionmaker"], manager, deps)
        run = await runner.create_stage5_run(request)
        result = await runner.execute_stage5(run.run_id, request)
    finally:
        await manager.close()

    # 每轮 audit rewrite → 修订 target section；超过 MAX 轮 → 不再重写，run FAILED。
    assert result["terminal"] == STAGE5_TERMINAL_REVISION_LIMIT_EXCEEDED
    assert result["revision_round"] == MAX_STAGE5_REVISION_ROUNDS + 1
    assert len(result["revisions"]) == MAX_STAGE5_REVISION_ROUNDS
    row = await _run_row(env["sessionmaker"], run.run_id)
    assert row["status"] == "failed"
    assert row["error_code"] == STAGE5_TERMINAL_REVISION_LIMIT_EXCEEDED
    assert await _revision_count(env["sessionmaker"]) == MAX_STAGE5_REVISION_ROUNDS
    # 修订 writer 每轮每个 target section 恰好调用一次。
    assert len(revision_model.calls) == MAX_STAGE5_REVISION_ROUNDS
    # 每轮装配新 Report（不 UPDATE 旧 Report，spec N）：原始 + 2 轮修订 = 3 份。
    assert await _run_count(env["sessionmaker"], "reports") == MAX_STAGE5_REVISION_ROUNDS + 1
    # 轮次递增。
    rounds = {rev["revision_round"] for rev in result["revisions"]}
    assert rounds == {1, 2}


# ---------------------------------------------------------------- human interrupt（spec Q/R）


async def test_human_interrupt_approve_durable_resume(env, monkeypatch, connection_uri) -> None:
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
        assert (await runner.get_run(run.run_id)).status.value == "waiting_human"
        assert result["route"] == "human_review"
        assert result["human_request_id"] is not None
        assert result["human_decision"] is None

        # durability：新 runner（同一 thread_id / checkpoint）恢复并裁决 approve。
        runner_b = Stage5WorkflowRunner(env["sessionmaker"], manager, deps)
        result = await runner_b.resume_stage5_human(run.run_id, "approve", comment=" 人工审核通过 ")
    finally:
        await manager.close()

    assert result["terminal"] == STAGE5_TERMINAL_FINALIZE_WITH_WARNINGS
    assert result["human_decision"] == "approve"
    assert (await runner.get_run(run.run_id)).status.value == "completed"
    assert await _revision_count(env["sessionmaker"]) == 0
    assert await _run_count(env["sessionmaker"], "human_review_decisions") == 1


async def test_human_interrupt_resume_rewrite_then_approve(
    env, monkeypatch, connection_uri
) -> None:
    """人工 rewrite → 新 Report → 再次 human_review interrupt → 人工 approve → finalize。"""
    synthesis_result_id = await _seed_synthesis(env, monkeypatch, connection_uri)
    request = _request(env, synthesis_result_id)
    deps = _stage5_deps(
        env["sessionmaker"],
        draft_model=FakeDraftSectionModel(decision_factory=valid_decision_for),
        audit_model=FakeAuditModel(decision_factory=human_review_decision),
        revision_model=FakeRevisionWriterModel(decision_factory=revision_decision_for),
    )
    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        runner = Stage5WorkflowRunner(env["sessionmaker"], manager, deps)
        run = await runner.create_stage5_run(request)
        await runner.execute_stage5(run.run_id, request)
        assert (await runner.get_run(run.run_id)).status.value == "waiting_human"

        # 1. 人工 rewrite → 修订 target section → 新 Report → 重新 check+audit →
        #    human_review 再次 interrupt（round 2）。
        result = await runner.resume_stage5_human(
            run.run_id, "rewrite", comment=" 请重新表述营收增长依据 "
        )
        assert len(result["revisions"]) == 1
        assert (await runner.get_run(run.run_id)).status.value == "waiting_human"
        assert result["revision_round"] == 2
        # 第二次 interrupt 的暂停态：rewrite_sections 已重置 decision，新一轮
        # human_request 已由 route_action 创建，decision 待新一轮人工裁决。
        assert result["human_decision"] is None
        assert result["human_request_id"] is not None

        # 2. 人工 approve（当前 Report 的 deterministic Check=pass）→ finalize。
        result = await runner.resume_stage5_human(run.run_id, "approve")
    finally:
        await manager.close()

    assert result["terminal"] == STAGE5_TERMINAL_FINALIZE_WITH_WARNINGS
    assert result["human_decision"] == "approve"
    assert (await runner.get_run(run.run_id)).status.value == "completed"
    assert await _revision_count(env["sessionmaker"]) == 1
    assert await _run_count(env["sessionmaker"], "reports") == 2
    assert await _run_count(env["sessionmaker"], "human_review_decisions") == 2


async def test_human_interrupt_resume_cancel(env, monkeypatch, connection_uri) -> None:
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
        await runner.execute_stage5(run.run_id, request)
        assert (await runner.get_run(run.run_id)).status.value == "waiting_human"

        result = await runner.resume_stage5_human(run.run_id, "cancel", comment=" 放弃本轮 ")
    finally:
        await manager.close()

    assert result["terminal"] == STAGE5_TERMINAL_CANCELLED
    assert result["human_decision"] == "cancel"
    assert (await runner.get_run(run.run_id)).status.value == "cancelled"
    assert await _revision_count(env["sessionmaker"]) == 0


# ---------------------------------------------------------------- research（spec S）


async def test_research_route_terminal(env, monkeypatch, connection_uri) -> None:
    """route=research → terminal research_required；不假装 research completed。"""
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
    assert result["route"] == "research"
    assert (await runner.get_run(run.run_id)).status.value == "completed"
    assert await _revision_count(env["sessionmaker"]) == 0


# ---------------------------------------------------------------- approve 安全（spec R，node 级）


async def test_approve_critical_alert_finalizes_with_warnings(
    env, monkeypatch, connection_uri
) -> None:
    """v1.2.5：critical 严重度（unsupported_by_evidence → CRITICAL_ALERT 严重审核
    提醒）不再阻断 approve——人工批准被接受 → terminal finalize_with_warnings
    （completed_with_warnings）。审核发现问题 ≠ 报告不可交付。"""
    outline_id = await _create_outline(env, monkeypatch, connection_uri, _two_theme_models())
    fake = FakeDraftSectionModel(decision_factory=valid_decision_for)
    draft_ids = await _draft_mixed_sections(
        env, outline_id, FakeDraftSectionModel(decision_factory=valid_decision_for)
    )
    report_service = ReportService(
        env["sessionmaker"], DraftSectionService(env["sessionmaker"], fake)
    )
    report = await report_service.create_or_get_report(
        ReportAssemblyDraft(outline_id=outline_id, draft_section_ids=tuple(draft_ids.values()))
    )
    check_service = ReportCheckService(env["sessionmaker"], report_service)
    check = await check_service.run_report_checks(report.report_id)
    assert check.status == "pass"
    # 对应 审计：critical issue（unsupported_by_evidence → 数据真实性无法确认/critical）。
    audit_model = FakeAuditModel(decision_factory=research_decision)
    audit_service = ReportAuditService(env["sessionmaker"], audit_model, check_service)
    audit = await audit_service.create_or_get_audit(
        ReportAuditRequest(report_id=report.report_id, check_result_id=check.check_result_id)
    )
    deps = _stage5_deps(
        env["sessionmaker"],
        draft_model=fake,
        audit_model=audit_model,
        revision_model=FakeRevisionWriterModel(),
    )
    node = make_finalize_on_approve_node(deps)
    result = await node(
        {
            "check_result_id": str(check.check_result_id),
            "audit_id": str(audit.audit_id),
        }
    )
    # v1.2.5：CRITICAL_ALERT 严重提醒 → 带警告完成（不阻断 approve）。
    assert result["terminal"] == STAGE5_TERMINAL_FINALIZE_WITH_WARNINGS


# ---------------------------------------------------------------- degrade approve（v1.2.2 §2 B）


async def test_approve_degraded_section_finalizes_with_warnings(
    env, monkeypatch, connection_uri
) -> None:
    """Check=fail 但全部 findings 归因 degraded section（model_unavailable 占位）
    → 人工 approve 被接受 → terminal finalize_with_warnings（不再是 run FAILED）。
    """
    from app.draft_section.contracts import DraftSectionRequest

    outline_id = await _create_outline(env, monkeypatch, connection_uri, _two_theme_models())
    fake = FakeDraftSectionModel(decision_factory=valid_decision_for)
    draft_service = DraftSectionService(env["sessionmaker"], fake)
    # S3（risks_and_gaps）标记 degraded：deterministic 诚实占位（0 LLM）。
    degraded = await draft_service.create_or_get_degraded_section(
        DraftSectionRequest(outline_id=outline_id, section_id="S3"),
        reason="model_unavailable",
    )
    # S1/S2 正常，S3 degraded → assemble 完整 Report（含诚实占位段落）。
    draft_ids: dict[str, UUID] = {}
    for section_id in ("S1", "S2"):
        result = await DraftSectionService(
            env["sessionmaker"], FakeDraftSectionModel(decision_factory=valid_decision_for)
        ).create_or_get_section(DraftSectionRequest(outline_id=outline_id, section_id=section_id))
        draft_ids[section_id] = result.draft_section_id
    draft_ids["S3"] = degraded.draft_section_id

    report_service = ReportService(env["sessionmaker"], draft_service)
    report = await report_service.create_or_get_report(
        ReportAssemblyDraft(outline_id=outline_id, draft_section_ids=tuple(draft_ids.values()))
    )
    check_service = ReportCheckService(env["sessionmaker"], report_service)
    check = await check_service.run_report_checks(report.report_id)
    assert check.status == "fail"

    deps = _stage5_deps(
        env["sessionmaker"],
        draft_model=fake,
        audit_model=FakeAuditModel(decision_factory=pass_decision),
        revision_model=FakeRevisionWriterModel(),
    )
    node = make_finalize_on_approve_node(deps)
    result = await node({"check_result_id": str(check.check_result_id)})
    assert result["terminal"] == STAGE5_TERMINAL_FINALIZE_WITH_WARNINGS


def _degrade_risks_gap_factory() -> object:
    """Factory: S2(risks_and_gaps) 抛 DraftSectionModelUnavailable，其余段正常。
    → drafting node 对该段确定性降级（reason=model_unavailable），S1/S2 正常。
    """
    from app.draft_section.packs import SectionInputPack

    def factory(pack: SectionInputPack):
        if pack.section_id == "S2":
            raise DraftSectionModelUnavailable()
        return valid_decision_for(pack)

    return factory


async def test_approve_degraded_workflow_completes_with_warnings(
    env, monkeypatch, connection_uri
) -> None:
    """全链路 Case2（§6）：degraded section + audit human_review → 人工 approve
    → run **completed**（terminal finalize_with_warnings），不再是 FAILED。
    """
    synthesis_result_id = await _seed_synthesis(env, monkeypatch, connection_uri)
    request = _request(env, synthesis_result_id)
    deps = _stage5_deps(
        env["sessionmaker"],
        draft_model=FakeDraftSectionModel(decision_factory=_degrade_risks_gap_factory()),
        audit_model=FakeAuditModel(decision_factory=human_review_decision),
        revision_model=FakeRevisionWriterModel(),
    )
    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        runner = Stage5WorkflowRunner(env["sessionmaker"], manager, deps)
        run = await runner.create_stage5_run(request)
        result = await runner.execute_stage5(run.run_id, request)
        assert (await runner.get_run(run.run_id)).status.value == "waiting_human"
        assert result["route"] == "human_review"

        runner_b = Stage5WorkflowRunner(env["sessionmaker"], manager, deps)
        result = await runner_b.resume_stage5_human(run.run_id, "approve", comment=" 人工确认占位 ")
    finally:
        await manager.close()

    assert result["terminal"] == STAGE5_TERMINAL_FINALIZE_WITH_WARNINGS
    assert result["human_decision"] == "approve"
    assert (await runner.get_run(run.run_id)).status.value == "completed"
    assert await _run_count(env["sessionmaker"], "human_review_decisions") == 1
