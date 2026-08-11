"""Task-level read-only artifact workspace tests (Stage 6B.1).

真实 PostgreSQL + 真实 LangGraph（PG Checkpointer）+ Fake LLM models，全程
**零真实 DeepSeek**。跑完整 Stage4 completed → Stage5 → approve → completed
链后，用 `TaskArtifactService` 验证 5 个只读方法 + `count_artifacts`：

1. **完整链产物**：analysis（work_items/claims/synthesis）、sources/evidence
   分页信封、report、reviews、count_artifacts 与各分项一致；evidence/source
   集为**任务级**（从 checkpoint 恢复，非 company 全集），所有卡/来源都属于
   研究任务涉及的公司范围（目标 + valuation peers，macro 可 NULL）——这正是
   任务级 scoped 的含义（该任务实际使用的证据），而非目标公司全集；
2. **task 不存在** → `TaskNotFound`；
3. **无 run 新任务** → 5 方法空 / null + counts 全 0（200 语义）。
"""

from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from app.core.config import get_settings
from app.core.errors import TaskNotFound
from app.core.runtime import configure_asyncio_runtime
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
from app.storage.raw_store import LocalRawArtifactStore
from app.synthesis.service import SynthesisService
from tests.integration.test_research_execution_recovery import (
    _get_run_status,
    _make_execution,
    _run_stage4_to_completed,
    _wait_for_stage5_waiting_human,
)
from tests.integration.test_revision_service import _cleanup_with_revisions
from tests.integration.test_stage4_workflow import _AS_OF, _QUESTION, _seed_research_task
from tests.integration.test_valuation_claim_service import _seed_company

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


async def _derive_expected_evidence(
    env,
    execution: ResearchExecutionService,
    stage4_run_id: UUID,
) -> tuple[set[UUID], set[UUID]]:
    """独立推导期望 evidence 集，返回 (work_item 输入集, 完整期望集)。

    完整期望集 = work item 输入证据 ∪ verified claims 的 evidence_card_ids（与
    TaskArtifactService 相同的 checkpoint 恢复路径，但测试侧独立组装）。只断言
    「期望集中实际存在于 evidence_cards 的行」，不依赖 fixture 中 valuation
    相关卡的数量波动。
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


# ---------------------------------------------------------------- full chain


async def test_full_chain_artifacts(env, monkeypatch, connection_uri) -> None:
    manager, stage4_run_id, _ = await _run_stage4_to_completed(env, monkeypatch, connection_uri)
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]
    company_id = env["company_id"]
    try:
        execution = _make_execution(sessionmaker, manager)
        try:
            coordinator = ResearchExecutionRecoveryCoordinator(sessionmaker, execution)
            assert await coordinator.recover_interrupted_chains() == 1
            stage5 = await _wait_for_stage5_waiting_human(sessionmaker, task_id)
            await execution.resume_human(UUID(stage5["run_id"]), "approve", "审核通过")
            assert await _get_run_status(sessionmaker, UUID(stage5["run_id"])) == "completed"
        finally:
            await execution.close()

        artifact = TaskArtifactService(sessionmaker, execution)

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
        assert len(analysis.claims) >= 2  # synthesis 合法下界
        assert all(c.claim_id for c in analysis.claims)

        # ---- evidence：任务级精确集 + 分页信封 ----
        work_item_input_ids, expected_evidence = await _derive_expected_evidence(
            env, execution, stage4_run_id
        )
        assert work_item_input_ids, "checkpoint 应恢复非空 work item 输入 evidence"
        async with sessionmaker() as session:
            existing_rows, existing_total = await EvidenceCardRepository(session).list_by_ids(
                sorted(expected_evidence, key=str), limit=len(expected_evidence), offset=0
            )
        existing_ids = {row.evidence_card_id for row in existing_rows}
        # work item 输入卡是 checkpoint 权威且必须真实存在；全部出现在返回集中。
        assert work_item_input_ids <= existing_ids, "work item 输入卡必须真实存在"
        assert existing_ids, "期望集应含实际存在的 evidence 卡"

        evidence = await artifact.get_evidence(task_id, limit=100, offset=0)
        assert isinstance(evidence, EvidenceArtifactListResponse)
        assert evidence.total == existing_total
        assert {e.evidence_card_id for e in evidence.items} == existing_ids
        # 任务级 scoped =「该任务实际使用的证据」，而非目标公司全集：valuation
        # comparison / macro 分析会引用 peer 公司（company_evidence_ids 等）的卡，
        # 因此允许全部卡都属于研究任务涉及的公司范围（目标 + peers，macro 可 NULL）。
        allowed_company_ids = {company_id, *env["peer_company_ids"]}
        assert all(
            e.company_id is None or e.company_id in allowed_company_ids
            for e in evidence.items
        )
        assert all(e.evidence_statement for e in evidence.items)

        # 分页切分正确（limit/offset 语义）。
        first_page = await artifact.get_evidence(task_id, limit=2, offset=0)
        assert first_page.total == evidence.total
        assert len(first_page.items) == 2
        assert first_page.items[0].evidence_card_id == evidence.items[0].evidence_card_id

        # ---- sources：从 evidence 反查 distinct source_id，全部属于目标公司 ----
        expected_sources = {row.source_id for row in existing_rows if row.source_id is not None}
        sources = await artifact.get_sources(task_id, limit=100, offset=0)
        assert isinstance(sources, SourceArtifactListResponse)
        assert sources.total == len(expected_sources)
        assert {s.source_id for s in sources.items} == expected_sources
        # 同上：source 集从 evidence 反查，peer 公司证据卡对应的 source 也属 peer。
        assert all(
            s.company_id is None or s.company_id in allowed_company_ids for s in sources.items
        )
        assert all(s.title for s in sources.items)

        # ---- report：verify_report_integrity read-side 投影 ----
        report = await artifact.get_report(task_id)
        assert isinstance(report, ReportArtifactResponse)
        assert report.report_id is not None
        assert report.outline_id is not None
        assert report.company_id == company_id
        assert report.report_fingerprint
        assert report.analysis_as_of == _AS_OF
        assert report.section_count and report.section_count > 0

        # ---- reviews：audit + issues（fake 决策 → critical issue → fail/human_review） ----
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

        # ---- count_artifacts：与各分项一致（任务级计数） ----
        summary = await artifact.count_artifacts(task_id)
        assert summary.source_count == sources.total
        assert summary.evidence_count == evidence.total
        assert summary.claim_count > 0
        assert summary.report_count == 1
        assert summary.review_issue_count == reviews.issue_count
    finally:
        await manager.close()


# ---------------------------------------------------------------- task not found


async def test_task_not_found(env) -> None:
    execution = _make_execution(env["sessionmaker"], None)
    artifact = TaskArtifactService(env["sessionmaker"], execution)
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


# ---------------------------------------------------------------- no run (empty)


async def test_no_run_task_returns_empty(env) -> None:
    new_task_id = await _seed_research_task(env["sessionmaker"])
    execution = _make_execution(env["sessionmaker"], None)
    artifact = TaskArtifactService(env["sessionmaker"], execution)

    sources = await artifact.get_sources(new_task_id, limit=20, offset=0)
    assert sources.items == [] and sources.total == 0

    evidence = await artifact.get_evidence(new_task_id, limit=20, offset=0)
    assert evidence.items == [] and evidence.total == 0

    analysis = await artifact.get_analysis(new_task_id)
    assert analysis.company_id is None
    assert analysis.work_items == [] and analysis.claims == []
    assert analysis.synthesis_id is None

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
