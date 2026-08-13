"""7A Product Gate 任务 G/H：真实受控 Source acquisition E2E（spec G/H）。

真实 PostgreSQL + 真实 LangGraph（PG Checkpointer）+ 真实顶层编排 + **真实
ResearchPreparationService / ResearchSourceRouter / SourceIngestionService /
SourceParsingService / ChunkingService / VectorIndexService / RetrievalService /
EvidenceExtractionService**。0 真实 DeepSeek / 0 live provider（仅允许
FakeEmbeddingProvider / FakeEvidenceExtractionModel / Fake Stage4/5 LLM）。

G1：真实 prep 因缺 2025 年度报告 → waiting_manual（missing=[annual_report_2025]）。
G2：调用真实 `SourceIngestionService.ingest_upload`（正式 PDF 上传，**禁止直接
  INSERT / `prep.sources_available=True`**）；最小机器生成 PDF fixture；断言
  RawArtifact + SourceRecord（company_id == orchestration verified company_id、
  content_sha256 存在、acquisition_method=user_upload / provider=sse /
  document_type=annual_report，Source Registry policy）。
G3：使用现有正式管线（parse → chunk → VectorIndexService → RetrievalService →
  EvidenceExtractionService）——禁止手动构造 ParsedSource/DocumentChunk/
  RetrievalHit/EvidenceCard；FakeEmbeddingProvider / FakeEvidenceExtractionModel
  允许；真实 Chroma（隔离 collection）。
G4：调用真实 `resume_after_source_acquisition(orchestration_id)` → 同一
  orchestration_id + 同一顶层 thread_id（仅 1 行 orchestration run、attempt_no=1、
  无 orchestration attempt2）；内部 Stage4 child attempt1 正常创建。
G5：waiting_manual → real PDF upload → RawArtifact → SourceRecord → production
  prep 可见 → same-thread resume → 离开 waiting_manual → Stage4 attempt1 → Stage5
  → completed。
H：Company B 上传正确 document_type 的 annual_report PDF → Company A prep 仍不
  满足（company 隔离）；A 自己的 source 允许继续 → resume → completed。
"""

from datetime import UTC, date, datetime
from io import BytesIO
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.domain.source_records import SourceDocumentType
from app.evidence.contracts import compute_research_question_sha256
from app.evidence.extractor.service import EvidenceExtractionService
from app.rag.index.service import VectorIndexService
from app.rag.retrieval.contracts import RetrievalQuery
from app.rag.retrieval.service import RetrievalService
from app.research_orchestration.dependencies import ResearchOrchestrationDependencies
from app.research_orchestration.service import ResearchOrchestrationChildService
from app.research_planning.preparation import ResearchPreparationService
from app.research_planning.router import ResearchSourceRouter
from app.research_planning.service import ResearchPlanningService
from app.services.chunking_service import ChunkingService
from app.services.company_identity_service import CompanyIdentityService
from app.services.source_ingestion_service import SourceIngestionService
from app.services.source_parsing_service import SourceParsingService
from app.services.source_registry_service import SourceRegistryService
from app.stage4.runner import Stage4WorkflowRunner
from app.stage5.runner import Stage5WorkflowRunner
from app.storage.raw_store import LocalRawArtifactStore
from app.synthesis.service import SynthesisService
from app.vectorstore.client import ChromaManager
from app.workflows.checkpoint import LangGraphCheckpointManager
from tests.audit.fakes import pass_decision
from tests.embedding.fakes import FakeEmbeddingProvider
from tests.integration.test_research_orchestration_product import _ProductHarness
from tests.integration.test_research_orchestration_stage5 import (
    _TEST_SPEC,
    _audit_model,
    _count,
    _drop_collection,
    _get_child,
    _get_orchestration_row,
    _PerHitExtractionModel,
    _runs_for_task,
    _stage5_deps_for,
)
from tests.integration.test_research_planning_service import (
    _QUESTION as _PLANNING_QUESTION,
)
from tests.integration.test_research_planning_service import (
    _plan_payload,
    _seed_research_task,
)
from tests.integration.test_revision_service import _cleanup_with_revisions
from tests.integration.test_stage4_workflow import (
    _build_deps as _stage4_deps,
)
from tests.integration.test_stage4_workflow import (
    _claim_count_for_company,
    _good_models,
    _seed_worker_inputs,
    _synthesis_counts,
)
from tests.integration.test_valuation_claim_service import _seed_company
from tests.pdf_fixtures import single_page_pdf
from tests.research_planning.fakes import FakeResearchPlannerModel

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()


# ---------------------------------------------------------------- cleanup / env


async def _cleanup(sessionmaker) -> None:
    """先删 orchestration / plan 层（FK RESTRICT），再走公共 Stage5 清理
    （`_cleanup_with_revisions` 内部会清 claims/evidence/sources/companies）。"""
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
async def chroma_manager() -> ChromaManager:
    settings = get_settings()
    manager = ChromaManager(
        host=settings.chroma_host,
        port=settings.chroma_port,
        ssl=settings.chroma_ssl,
        timeout_seconds=settings.chroma_timeout_seconds,
    )
    yield manager


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


# ---------------------------------------------------------------- G plan / deps


def _g_payload():
    """G plan：base 全部 needs + annual_report_2025（唯一缺 document need）。

    `_seed_worker_inputs(research_question=<planning _QUESTION>)` 解析除
    annual_report_2025 外的全部 needs；annual_report 走真实 PDF acquisition。
    """
    return _plan_payload(
        document_needs=[
            {
                "need_code": "news_docs",
                "purpose": "需要公司新闻",
                "source_type": "news_article",
            },
            {
                "need_code": "annual_report_2025",
                "purpose": "需要2025年年度报告",
                "source_type": "annual_report",
                "period": "2025",
            },
        ],
    )


def _g_deps(sessionmaker, manager, *, audit_model) -> ResearchOrchestrationDependencies:
    """真实 preparation / router / plan（Fake planner 只产 G payload）+ 真实
    Stage4/Stage5 runner（LLM 全部 Fake）。"""
    plan_service = ResearchPlanningService(
        sessionmaker,
        FakeResearchPlannerModel(payload=_g_payload()),
        CompanyIdentityService(sessionmaker),
    )
    router = ResearchSourceRouter(sessionmaker, plan_service)
    preparation = ResearchPreparationService(sessionmaker, plan_service, router)
    stage4_deps = _stage4_deps(sessionmaker, _good_models())
    stage4_runner = Stage4WorkflowRunner(sessionmaker, manager, stage4_deps)
    stage5_runner = Stage5WorkflowRunner(
        sessionmaker,
        manager,
        _stage5_deps_for(sessionmaker, audit_model),
    )
    child_service = ResearchOrchestrationChildService(
        sessionmaker, stage4_runner, stage5_runner=stage5_runner
    )
    return ResearchOrchestrationDependencies(
        sessionmaker=sessionmaker,
        plan_service=plan_service,
        router=router,
        preparation=preparation,
        fulfillment=_FakeFulfillment(),
        child_service=child_service,
        stage4_runner=stage4_runner,
        synthesis_service=SynthesisService(sessionmaker),
        stage5_runner=stage5_runner,
        backflow_service=stage5_runner.dependencies.research_backflow_service,
        backflow_executor=None,
    )


class _FakeFulfillment:
    """记录调用的 fulfill（G 场景不触发补资料，readiness 由真实 prep 控制）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def fulfill_research_needs(self, research_plan_id):
        self.calls += 1


# ---------------------------------------------------------------- real upload helper


async def _upload_annual_report(
    env: dict, *, company_id, title: str = "贵州茅台2025年年度报告"
) -> tuple:
    """G2：真实 `SourceIngestionService.ingest_upload`（正式 PDF 上传）。

    - 最小机器生成 PDF（`tests.pdf_fixtures`，0 真实网络 / 0 第三方 PDF 运行时）；
    - `published_at` <= analysis_as_of（2026-08-10），`reporting_period_end` =
      2025-12-31，`source_url` ∈ sse.com.cn 受控域名（Source Registry policy）。
    返回 (IngestionResult, pdf_bytes)。
    """
    pdf_bytes = single_page_pdf(title=title)
    result = await SourceIngestionService(
        sessionmaker=env["sessionmaker"],
        raw_store=env["raw_store"],
    ).ingest_upload(
        company_id=company_id,
        provider_key="sse",
        document_type=SourceDocumentType.ANNUAL_REPORT,
        title=title,
        source_url="https://static.sse.com.cn/2025/annual_report.pdf",
        published_at=datetime(2026, 8, 5, tzinfo=UTC),
        reporting_period_end=date(2025, 12, 31),
        external_document_id=None,
        stream=BytesIO(pdf_bytes),
    )
    return result, pdf_bytes


async def _real_evidence_from_pdf(
    env: dict,
    *,
    company_id: UUID,
    source_id,
    chroma_manager: ChromaManager,
    collection_name: str,
    research_question: str,
) -> tuple:
    """G3：现有正式管线（parse → chunk → index → retrieve → extract）。

    返回 (extractor, new_card_ids)；extractor.calls 证明 hit 来自真实检索链
    （不手工构造 RetrievalHit / EvidenceCard）。
    """
    sessionmaker = env["sessionmaker"]
    parsed = await SourceParsingService(sessionmaker, env["raw_store"]).parse_source(source_id)
    chunked = await ChunkingService(sessionmaker).chunk_parsed_source(parsed.parsed_source_id)
    embedding = FakeEmbeddingProvider(_TEST_SPEC)
    await VectorIndexService(
        sessionmaker=sessionmaker,
        embedding_provider=embedding,
        chroma=chroma_manager,
        collection_name=collection_name,
    ).index_chunk_set(chunked.chunk_set_id)
    retrieval = RetrievalService(
        sessionmaker=sessionmaker,
        embedding_provider=embedding,
        chroma=chroma_manager,
        collection_name=collection_name,
    )
    hits = await retrieval.retrieve(
        RetrievalQuery(
            company_id=company_id,
            query_text=research_question,
            top_k=1,
        )
    )
    assert hits, "real retrieval must return the uploaded annual-report chunk"
    hit = hits[0]
    assert hit.source_id == source_id
    assert hit.company_id == company_id
    assert hit.document_type == "annual_report"

    extractor = _PerHitExtractionModel()
    extraction = await EvidenceExtractionService(sessionmaker, extractor).extract_from_hit(
        research_question, hit
    )
    assert extraction.relevant, "extractor must produce relevant evidence"
    assert extraction.evidence_card_ids
    assert extractor.calls, "extractor must receive a real RetrievalHit"
    return extractor, list(extraction.evidence_card_ids)


async def _assert_uploaded_source_shape(result, *, company_id) -> None:
    """G2 断言：RawArtifact（content_sha256 存在）+ SourceRecord 的 registry policy。"""
    record = result.record
    assert result.replayed is False
    assert record.source_id
    assert record.company_id == company_id
    assert record.content_sha256  # RawArtifact 已归档（内容寻址 SHA 存在）
    assert record.byte_size > 0
    assert record.media_type == "application/pdf"
    assert record.provider_key == "sse"
    assert record.document_type == SourceDocumentType.ANNUAL_REPORT
    assert record.acquisition_method == "user_upload"
    assert record.reporting_period_end == date(2025, 12, 31)
    assert record.status == "available"


# ---------------------------------------------------------------- G1-G5


async def test_g_real_controlled_acquisition_e2e(
    env, monkeypatch, connection_uri, chroma_manager
) -> None:
    """G1-G5：waiting_manual → real PDF upload → real pipeline → same-thread resume
    → Stage4 attempt1 → Stage5 → completed（同一 orchestration_id / 顶层 thread）。"""
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]
    company_id = env["company_id"]
    # Gate C：seed 其它 needs 的卡必须绑定 **planning** research_question（与
    # plan 冻结 question 同 hash）——不能用 stage4 默认 _QUESTION。
    ids = await _seed_worker_inputs(env, monkeypatch, research_question=_PLANNING_QUESTION)
    assert ids  # 5 类 worker 输入已 seed

    collection_name = f"test_acq_e2e_{uuid4().hex[:12]}"
    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    client = await chroma_manager.get_client()
    harness = None
    try:
        deps = _g_deps(sessionmaker, manager, audit_model=_audit_model(pass_decision))
        harness = _ProductHarness(sessionmaker, deps, manager)

        # G1：真实 prep 因缺 2025 annual report → waiting_manual。
        outcome = await harness.start(task_id)
        assert outcome.created is True
        assert outcome.scheduled is True
        o1 = outcome.orchestration.orchestration_id
        await harness.wait_idle(o1, message="G1 waiting_manual 未在超时前到达")
        row = await _get_orchestration_row(sessionmaker, o1)
        assert row["status"] == "waiting_human"
        assert row["current_phase"] == "waiting_manual"
        proj = await harness.service.get_orchestration(o1)
        assert proj.missing_need_codes == ["annual_report_2025"]
        assert proj.manual_reason is None
        # 0 个 child run（waiting_manual 不做研究）。
        assert await _count(sessionmaker, "workflow_runs") == 0

        # G2：真实 SourceIngestionService 正式 PDF 上传。
        result, _pdf = await _upload_annual_report(env, company_id=company_id)
        await _assert_uploaded_source_shape(result, company_id=company_id)
        src_id = result.record.source_id
        # SourceRecord 的 company_id 必须 == orchestration verified company_id。
        assert result.record.company_id == company_id

        # G3：现有正式管线（real Chroma，隔离 collection）。
        extractor, card_ids = await _real_evidence_from_pdf(
            env,
            company_id=company_id,
            source_id=src_id,
            chroma_manager=chroma_manager,
            collection_name=collection_name,
            research_question=_PLANNING_QUESTION,
        )
        # Gate C：新卡 research_question_sha256 == 冻结 plan question hash。
        rq_sha = compute_research_question_sha256(_PLANNING_QUESTION)
        async with sessionmaker() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT evidence_card_id::text, research_question_sha256, "
                        "source_id::text, company_id::text FROM evidence_cards "
                        "WHERE evidence_card_id = ANY(:ids)"
                    ).bindparams(ids=card_ids)
                )
            ).mappings()
            cards = [dict(r) for r in rows]
        assert len(cards) == len(card_ids)
        for card in cards:
            assert card["research_question_sha256"] == rq_sha
            assert card["source_id"] == str(src_id)
            assert card["company_id"] == str(company_id)

        # G4：真实 resume（同一 orchestration_id + 顶层 thread）。
        resume = await harness.service.resume_after_source_acquisition(o1)
        assert resume.orchestration_id == o1
        await harness.wait_idle(o1, message="G4 resume 未在超时前完成")

        # G5：completed；仅 1 行 orchestration、attempt_no=1、无 retry。
        row = await _get_orchestration_row(sessionmaker, o1)
        assert row["status"] == "completed"
        assert row["current_phase"] == "completed"
        assert row["error_code"] is None
        assert await _count(sessionmaker, "research_orchestration_runs") == 1
        assert row["attempt_no"] == 1
        assert row["retry_of_orchestration_id"] is None
        # 内部 Stage4/Stage5 child attempt1 正常创建（无 attempt2）。
        child4 = await _get_child(sessionmaker, o1, "stage4", attempt_no=1)
        child5 = await _get_child(sessionmaker, o1, "stage5", attempt_no=1)
        assert child4 is not None and child5 is not None
        assert await _get_child(sessionmaker, o1, "stage4", attempt_no=2) is None
        runs = await _runs_for_task(sessionmaker, task_id)
        assert len(runs) == 2
        assert {r["graph_name"] for r in runs} == {"stage4_analysis", "stage5_report"}
        assert all(r["status"] == "completed" for r in runs)
        # 真实产物：5 claims + 1 synthesis + 1 report。
        assert await _claim_count_for_company(sessionmaker, company_id) == 5
        assert await _synthesis_counts(sessionmaker) == (1, 1)
        assert await _count(sessionmaker, "reports") == 1
    finally:
        await _drop_collection(client, collection_name)
        if harness is not None:
            await harness.close()
        await manager.close()


# ---------------------------------------------------------------- H：company isolation


async def test_h_company_isolation_negative(
    env, monkeypatch, connection_uri, chroma_manager
) -> None:
    """H：Company B 上传正确 document_type 的 annual_report PDF → Company A prep
    仍不满足（company 隔离，`_load_resolution_data(company_id)` 过滤）；A 自己的
    source 允许继续 → resume → completed。"""
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]
    company_id = env["company_id"]
    await _seed_worker_inputs(env, monkeypatch, research_question=_PLANNING_QUESTION)

    collection_name = f"test_acq_iso_{uuid4().hex[:12]}"
    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    client = await chroma_manager.get_client()
    harness = None
    try:
        deps = _g_deps(sessionmaker, manager, audit_model=_audit_model(pass_decision))
        harness = _ProductHarness(sessionmaker, deps, manager)

        outcome = await harness.start(task_id)
        assert outcome.created is True
        o1 = outcome.orchestration.orchestration_id
        await harness.wait_idle(o1, message="H waiting_manual 未在超时前到达")
        row = await _get_orchestration_row(sessionmaker, o1)
        assert row["current_phase"] == "waiting_manual"
        plan_id = row["research_plan_id"]
        proj = await harness.service.get_orchestration(o1)
        assert proj.missing_need_codes == ["annual_report_2025"]

        # Company B（另一公司）上传正确 annual_report PDF。
        company_b = await _seed_company(sessionmaker, "600501")
        result_b, _ = await _upload_annual_report(
            env, company_id=company_b, title="B公司2025年年度报告"
        )
        await _assert_uploaded_source_shape(result_b, company_id=company_b)

        # 真实 prep：A 的 plan 仍缺 annual_report_2025（company 隔离）。
        prep_b = await deps.preparation.prepare_research(plan_id)
        assert prep_b.ready_for_analysis is False
        missing_codes_b = [n.need_code for n in prep_b.missing_needs]
        assert "annual_report_2025" in missing_codes_b
        assert await _count(sessionmaker, "research_orchestration_runs") == 1

        # Company A 上传自己的 annual_report → prep 现在 ready。
        result_a, _ = await _upload_annual_report(env, company_id=company_id)
        await _assert_uploaded_source_shape(result_a, company_id=company_id)
        # 真实管线（index + extract），让 A 的证据卡带正确 research_question hash。
        await _real_evidence_from_pdf(
            env,
            company_id=company_id,
            source_id=result_a.record.source_id,
            chroma_manager=chroma_manager,
            collection_name=collection_name,
            research_question=_PLANNING_QUESTION,
        )
        prep_a = await deps.preparation.prepare_research(plan_id)
        assert prep_a.ready_for_analysis is True
        assert prep_a.missing_needs == ()

        # A 自己的 source 允许继续 → 同 orchestration resume → completed。
        resume = await harness.service.resume_after_source_acquisition(o1)
        assert resume.orchestration_id == o1
        await harness.wait_idle(o1, message="H resume 未在超时前完成")
        row = await _get_orchestration_row(sessionmaker, o1)
        assert row["status"] == "completed"
        assert row["current_phase"] == "completed"
        assert await _count(sessionmaker, "research_orchestration_runs") == 1
        assert row["attempt_no"] == 1
        runs = await _runs_for_task(sessionmaker, task_id)
        assert len(runs) == 2
        assert all(r["status"] == "completed" for r in runs)
        assert await _claim_count_for_company(sessionmaker, company_id) == 5
        assert await _count(sessionmaker, "reports") == 1
    finally:
        await _drop_collection(client, collection_name)
        if harness is not None:
            await harness.close()
        await manager.close()
