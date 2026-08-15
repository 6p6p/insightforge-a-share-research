"""V1.1 Fresh Product Acceptance（本轮最重要新 Gate）。

目标：第一次真正证明

    fresh PostgreSQL → startup bootstrap → 普通用户流程 → report/citation

**测试中禁止**（违反即失败）：
- CompanyRepository.create / seed company fixture / seed_defaults fixture——
  公司主数据与 source registry 全部来自 production bootstrap（bundled snapshot）；
- controlled plan / manual parse() / manual chunk() / manual index() /
  manual extract()——资料供给走 production SourcePreparationService /
  ResearchFulfillmentService（真实检索链 + fake LLM/extractor/embedding）。

覆盖：
- A. Fresh Company Bootstrap：临时 fresh DB → alembic head → registry + master
  bootstrap → companies > 5000、aliases > companies、snapshot provenance 行；
  贵州茅台 / 宁德时代 八种输入形态 resolve；非法/未收录/跨所重名 ambiguous。
- B. Fresh Task Path：company_query=宁德时代 + 单问题 → create task →
  orchestration（fake planner）→ identity resolved → planning 进入（plan 行）。
- C. Manual Source Completion：编排 waiting_manual → 生产 ingestion 上传 PDF →
  production source preparation（parse→chunk→index）→ resume 同线程 →
  fulfill（真实 Retrieval/Chroma + fake extractor）→ evidence → Stage4（fake
  models）→ synthesis → Stage5（fake draft/audit pass）→ completed。
- D. Report Path：report 存在、claim↔evidence↔source 引用链完整（citation）。

全程 0 real DeepSeek / 0 real external network（conftest Network Guard 兜底；
Chroma 为本地回环测试实例，embedding 用 FakeEmbeddingProvider + 隔离 collection）。
"""

import asyncio
import io
import os
import time
import uuid
from datetime import date
from pathlib import Path
from uuid import UUID

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import text

from alembic import command
from app.analysis.synthesis.contracts import (
    SynthesisAnalysisOutput,
    SynthesisClaimRole,
    SynthesisClaimRoleAssignment,
    SynthesisEvidenceGap,
    SynthesisPriority,
    SynthesisTheme,
)
from app.companies.master.snapshot import load_bundled_snapshot
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.domain.source_records import SourceDocumentType
from app.domain.tasks import ResearchModule
from app.evidence.contracts import EvidenceConfidence, EvidenceType
from app.evidence.extractor.contracts import (
    EvidenceExtractionDecision,
    EvidenceExtractionItem,
    EvidenceExtractionReason,
)
from app.rag.embedding.contracts import EmbeddingModelSpec
from app.rag.index.service import VectorIndexService
from app.rag.retrieval.service import RetrievalService
from app.repositories.research_task_repository import ResearchTaskRepository
from app.research_fulfillment.executors import (
    DocumentNeedExecutor,
    FinancialNeedExecutor,
    MacroNeedExecutor,
    SourceIndexBuilder,
    ValuationNeedExecutor,
)
from app.research_fulfillment.service import ResearchFulfillmentService
from app.research_orchestration.contracts import (
    OrchestrationPhase,
    OrchestrationStatus,
)
from app.research_orchestration.dependencies import ResearchOrchestrationDependencies
from app.research_orchestration.execution_manager import ResearchOrchestrationExecutionManager
from app.research_orchestration.runner import ResearchOrchestrationRunner
from app.research_orchestration.service import (
    ResearchOrchestrationChildService,
    ResearchOrchestrationService,
)
from app.research_planning.contracts import ResearchPlanPayload
from app.research_planning.planner import ResearchPlannerModel
from app.research_planning.preparation import ResearchPreparationService
from app.research_planning.router import ResearchSourceRouter
from app.research_planning.service import ResearchPlanningService
from app.schemas.task import TaskCreateRequest
from app.services.chunking_service import ChunkingService
from app.services.company_identity_service import CompanyIdentityService
from app.services.company_master_service import CompanyMasterBootstrapService
from app.services.source_ingestion_service import SourceIngestionService
from app.services.source_preparation_service import SourcePreparationService
from app.services.source_registry_service import SourceRegistryService
from app.services.task_service import TaskService
from app.stage4.runner import Stage4WorkflowRunner as Stage4Runner
from app.stage5.runner import Stage5WorkflowRunner as Stage5Runner
from app.storage.raw_store import LocalRawArtifactStore
from app.synthesis.service import SynthesisService
from app.vectorstore.client import ChromaManager
from app.workflows.checkpoint import LangGraphCheckpointManager
from tests.audit.fakes import FakeAuditModel, pass_decision
from tests.draft_section.fakes import FakeDraftSectionModel, valid_decision_for
from tests.embedding.fakes import FakeEmbeddingProvider
from tests.evidence.fakes import FakeEvidenceExtractionModel
from tests.integration.research_fulfillment_helpers import _unique_quote
from tests.integration.test_stage4_workflow import (
    _build_deps as _stage4_deps,
)
from tests.integration.test_stage4_workflow import (
    _good_models,
)
from tests.integration.test_stage5_workflow import _stage5_deps
from tests.pdf_fixtures import single_page_pdf
from tests.revision.fakes import FakeRevisionWriterModel

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

_QUESTION = "宁德时代近三年的盈利能力和增长驱动发生了什么变化？"
_AS_OF = date(2026, 8, 14)
_START = date(2023, 1, 1)

# 只含 document need（annual_report 2023）：business/risk 模块输入 = 文档证据池
# （Stage4 要求最低 claim 数，两个模块共享同一池 → 2 条 claim）；无
# financial/macro/valuation need（fresh 环境无观测数据导入路径，保持真实）。
_PLAN_PAYLOAD = ResearchPlanPayload.model_validate(
    {
        "research_scope": ["business", "risk"],
        "analysis_modules": ["business_event", "risk"],
        "document_needs": [
            {
                "need_code": "annual_docs",
                "purpose": "需要年度报告",
                "source_type": "annual_report",
                "period": "2023",
            }
        ],
        "financial_needs": [],
        "macro_needs": [],
        "event_needs": [],
        "valuation_needs": [],
        "research_focus": ["经营质量"],
    }
)

_TEST_SPEC = EmbeddingModelSpec(
    model_id="BAAI/bge-small-zh-v1.5",
    dimension=512,
    normalize_embeddings=True,
    query_instruction="为这个句子生成表示以用于检索相关文章：",
    max_input_tokens=512,
    revision="test-revision-fresh-001",
)


class _FakePlanner(ResearchPlannerModel):
    """确定性 planner：返回固定 document-only payload（0 LLM）。"""

    model_id = "fake:fresh-planner"

    async def generate(self, request) -> ResearchPlanPayload:
        return _PLAN_PAYLOAD


class _DynamicSynthesisModel:
    """确定性 synthesis：按真实 claim pack 的 alias 集合输出（C1..Cn）。"""

    model_id = "fake:fresh-synthesis"

    async def analyze(self, context, claim_pack) -> SynthesisAnalysisOutput:
        refs = list(claim_pack.alias_map().keys())
        return SynthesisAnalysisOutput(
            summary="综合判断：文档证据支持经营基本面结论。",
            themes=[
                SynthesisTheme(
                    title="多维证据支持",
                    summary="业务证据指向一致结论。",
                    claim_refs=refs,
                )
            ],
            claim_roles=[
                SynthesisClaimRoleAssignment(
                    claim_ref=ref,
                    role=SynthesisClaimRole.SUPPORT,
                    rationale=f"支持 {ref}",
                )
                for ref in refs
            ],
            duplicates=[],
            conflicts=[],
            evidence_gaps=[
                SynthesisEvidenceGap(
                    description="缺少现金流证据",
                    claim_refs=refs[:1],
                    suggested_evidence="经营现金流数据",
                    priority=SynthesisPriority.MEDIUM,
                )
            ],
        )


class _PerHitExtractionModel(FakeEvidenceExtractionModel):
    """对每个真实 RetrievalHit 按其文本生成确定性 decision（quote 唯一可解析）。"""

    async def extract(self, research_question: str, retrieval_hit):
        self.calls.append((research_question, retrieval_hit))
        text_value = retrieval_hit.text
        if not any(text_value[i] != text_value[i - 1] for i in range(1, len(text_value))):
            return EvidenceExtractionDecision(
                relevant=False, items=[], reason_code=EvidenceExtractionReason.NOT_RELEVANT
            )
        return EvidenceExtractionDecision(
            relevant=True,
            items=[
                EvidenceExtractionItem(
                    evidence_statement="宁德时代发布年度经营相关材料。",
                    evidence_type=EvidenceType.METRIC,
                    quote_text=_unique_quote(text_value, 20),
                    confidence=EvidenceConfidence.HIGH,
                )
            ],
        )


# ------------------------------------------------------------------ fresh DB


async def _upgrade_head() -> None:
    cfg = Config(str(ALEMBIC_INI))
    await asyncio.to_thread(command.upgrade, cfg, "head")


def _parse_db_url(url: str) -> dict:
    from urllib.parse import urlparse

    parsed = urlparse(url.replace("+psycopg", "", 1))
    return {
        "host": parsed.hostname,
        "port": parsed.port,
        "user": parsed.username,
        "password": parsed.password,
        "dbname": parsed.path.lstrip("/"),
    }


def _admin_conn(db_name: str):
    import psycopg

    parts = _parse_db_url(get_settings().database_url)
    return psycopg.connect(
        host=parts["host"],
        port=parts["port"],
        user=parts["user"],
        password=parts["password"],
        dbname=db_name,
        autocommit=True,
    )


@pytest_asyncio.fixture(scope="module")
async def fresh_env(tmp_path_factory):
    """临时 fresh DB：alembic head → registry+master bootstrap → 返回 env。

    **不 seed 任何 company / provider**——全部来自 production bootstrap。
    """
    settings = get_settings()
    shared = settings.database_url
    temp_db = f"insightforge_fresh_{uuid.uuid4().hex[:10]}"
    temp_url = shared.rsplit("/", 1)[0] + f"/{temp_db}"
    with _admin_conn("postgres") as conn:
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{temp_db}"')
    previous_env = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = temp_url
    get_settings.cache_clear()
    try:
        await _upgrade_head()
    finally:
        if previous_env is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_env
        get_settings.cache_clear()

    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=10)
    try:
        sessionmaker = manager.session_factory()
        raw_store = LocalRawArtifactStore(
            root=tmp_path_factory.mktemp("raw"), max_bytes=16 * 1024 * 1024
        )
        # production bootstrap 顺序：registry → company master。
        await SourceRegistryService(sessionmaker).seed_defaults()
        bootstrap = await CompanyMasterBootstrapService(sessionmaker).bootstrap()
        env = {
            "sessionmaker": sessionmaker,
            "raw_store": raw_store,
            "temp_url": temp_url,
            "bootstrap": bootstrap,
        }
        yield env
    finally:
        await manager.dispose()
        with _admin_conn("postgres") as conn:
            with conn.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{temp_db}" WITH (FORCE)')


async def _count(sessionmaker, table: str) -> int:
    async with sessionmaker() as session:
        return int((await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())


async def _create_task(sessionmaker, *, question: str = _QUESTION) -> object:
    """创建 fresh 任务（V1.1 closure：modules 覆盖 payload 的 business_event/risk）。"""
    async with sessionmaker() as session:
        task_service = TaskService(ResearchTaskRepository(session))
        result = await task_service.create_task(
            TaskCreateRequest(
                company_query="宁德时代",
                research_start_date=_START,
                research_end_date=_AS_OF,
                modules=[
                    ResearchModule.COMPANY_PROFILE,
                    ResearchModule.BUSINESS,
                    ResearchModule.EVENTS,
                    ResearchModule.RISK,
                ],
                questions=[question],
                include_relative_valuation=False,
                require_plan_approval=False,
            ),
            None,
        )
        await session.commit()
        return result


# ================================================================== A


async def test_fresh_company_master_bootstrap(fresh_env) -> None:
    """A. fresh DB → bootstrap → 主数据可用 + provenance 记录 + 幂等。"""
    sessionmaker = fresh_env["sessionmaker"]
    bootstrap = fresh_env["bootstrap"]
    assert bootstrap.skipped is False
    assert bootstrap.imported_companies > 5000, bootstrap.imported_companies
    companies = await _count(sessionmaker, "companies")
    aliases = await _count(sessionmaker, "company_aliases")
    assert companies > 5000, companies
    assert aliases > companies, (aliases, companies)
    assert await _count(sessionmaker, "company_master_snapshots") == 1

    # 幂等：再次 bootstrap → skip；同 snapshot 再 import → replay 0 写。
    again = await CompanyMasterBootstrapService(sessionmaker).bootstrap()
    assert again.skipped is True
    replayed = await CompanyMasterBootstrapService(sessionmaker).import_snapshot(
        load_bundled_snapshot()
    )
    assert replayed.replayed is True
    assert await _count(sessionmaker, "companies") == companies


async def test_fresh_company_resolution(fresh_env) -> None:
    """A. 贵州茅台 / 宁德时代 八种形态全部 resolve；错误路径稳定。"""
    sessionmaker = fresh_env["sessionmaker"]
    service = CompanyIdentityService(sessionmaker)

    cases = [
        ("贵州茅台", "600519"),
        ("600519", "600519"),
        ("600519.SH", "600519"),
        ("SSE:600519", "600519"),
        ("宁德时代", "300750"),
        ("300750", "300750"),
        ("300750.SZ", "300750"),
        ("SZSE:300750", "300750"),
    ]
    for query, expected_code in cases:
        result = await service.resolve(query)
        assert result.company.security_code == expected_code, query
        assert result.match_type.value in (
            "official_name",
            "short_name",
            "security_code",
            "explicit_symbol",
            "identity_key",
        )

    from app.core.errors import CompanyIdentityAmbiguous, CompanyIdentityNotFound

    for bad in ("999999", "999999.SH", "SSE:999999", "不存在公司名称XYZ"):
        with pytest.raises(CompanyIdentityNotFound):
            await service.resolve(bad)
    # 跨所真实重名（三维股份：SSE 603033 / BSE 920834）→ ambiguous 语义。
    with pytest.raises(CompanyIdentityAmbiguous):
        await service.resolve("三维股份")


# ================================================================== B


async def test_fresh_task_path_enters_planning(fresh_env) -> None:
    """B. 宁德时代 + 单问题 → create task → orchestration（fake planner）→ planning。"""
    sessionmaker = fresh_env["sessionmaker"]
    task = await _create_task(sessionmaker)
    assert task.task.status.value == "pending"

    plan_service = ResearchPlanningService(
        sessionmaker, _FakePlanner(), CompanyIdentityService(sessionmaker)
    )
    orchestration_service = ResearchOrchestrationService(sessionmaker, plan_service)
    result = await orchestration_service.create_or_get_orchestration(task.task.task_id)
    assert result.replayed is False
    assert result.research_plan_id is not None
    assert result.status == OrchestrationStatus.PENDING.value
    assert result.current_phase == OrchestrationPhase.PLANNING.value

    from app.db.models.research_plan import ResearchPlanModel

    async with sessionmaker() as session:
        plan = await session.get(ResearchPlanModel, result.research_plan_id)
    assert plan is not None
    assert plan.company_id is not None  # identity resolved，非 company_identity_not_found


# ================================================================== C + D


async def test_fresh_source_completion_to_report(fresh_env) -> None:
    """C+D. waiting_manual → 生产上传 → production preparation → resume 同线程
    → fulfill（真实检索链）→ evidence → Stage4 → synthesis → Stage5 → report
    → citation 链完整。全程不手工 parse/chunk/index/extract。"""
    sessionmaker = fresh_env["sessionmaker"]
    raw_store = fresh_env["raw_store"]

    # ---- 任务：宁德时代（公司来自 master bootstrap）
    task = await _create_task(sessionmaker)
    company = (await CompanyIdentityService(sessionmaker).resolve("宁德时代")).company
    # ---- 真实检索链依赖（FakeEmbedding + 隔离 collection；真实 Chroma 回环）
    chroma = ChromaManager(host="127.0.0.1", port=8002, timeout_seconds=10)
    collection_name = f"fresh_{uuid.uuid4().hex[:12]}"
    embedding = FakeEmbeddingProvider(spec=_TEST_SPEC)
    vector_index = VectorIndexService(
        sessionmaker, embedding, chroma, collection_name=collection_name
    )
    retrieval = RetrievalService(sessionmaker, embedding, chroma, collection_name=collection_name)

    # ---- 真实 plan/prepare/fulfill（fake planner / fake extractor）
    plan_service = ResearchPlanningService(
        sessionmaker, _FakePlanner(), CompanyIdentityService(sessionmaker)
    )
    router = ResearchSourceRouter(sessionmaker, plan_service)
    preparation = ResearchPreparationService(sessionmaker, plan_service, router)
    chunking = ChunkingService(sessionmaker)
    source_preparation = SourcePreparationService(sessionmaker, raw_store, chunking, vector_index)
    index_builder = SourceIndexBuilder(
        sessionmaker,
        chunking,
        vector_index,
        parsing_service=source_preparation.parsing_service,
    )
    extractor = _PerHitExtractionModel()
    document_executor = DocumentNeedExecutor(
        sessionmaker, retrieval, extractor, index_builder=index_builder
    )
    fulfillment = ResearchFulfillmentService(
        sessionmaker,
        plan_service,
        router,
        preparation,
        document_executor=document_executor,
        financial_executor=FinancialNeedExecutor(sessionmaker),
        macro_executor=MacroNeedExecutor(sessionmaker),
        valuation_executor=ValuationNeedExecutor(),
    )

    # ---- Stage4/Stage5（fake models）+ 顶层编排
    checkpoint = LangGraphCheckpointManager(
        connection_uri=to_postgres_connection_uri(fresh_env["temp_url"])
    )
    await checkpoint.setup()
    models = {**_good_models(), "synthesis": _DynamicSynthesisModel()}
    stage4_runner = Stage4Runner(sessionmaker, checkpoint, _stage4_deps(sessionmaker, models))
    stage5_runner = Stage5Runner(
        sessionmaker,
        checkpoint,
        _stage5_deps(
            sessionmaker,
            draft_model=FakeDraftSectionModel(decision_factory=valid_decision_for),
            audit_model=FakeAuditModel(decision_factory=pass_decision),
            revision_model=FakeRevisionWriterModel(),
        ),
    )
    child_service = ResearchOrchestrationChildService(
        sessionmaker, stage4_runner, stage5_runner=stage5_runner
    )
    deps = ResearchOrchestrationDependencies(
        sessionmaker=sessionmaker,
        plan_service=plan_service,
        router=router,
        preparation=preparation,
        fulfillment=fulfillment,
        child_service=child_service,
        stage4_runner=stage4_runner,
        synthesis_service=SynthesisService(sessionmaker),
        stage5_runner=stage5_runner,
        backflow_service=stage5_runner.dependencies.research_backflow_service,
        backflow_executor=None,
    )
    orchestration_runner = ResearchOrchestrationRunner(sessionmaker, checkpoint, deps)
    manager = ResearchOrchestrationExecutionManager(orchestration_runner)
    orchestration_service = ResearchOrchestrationService(
        sessionmaker,
        plan_service,
        stage5_runner=stage5_runner,
        orchestration_runner=orchestration_runner,
        execution_manager=manager,
        source_preparation=source_preparation,
    )
    try:
        # ---- 一键入口 → 后台真实顶层图 → 缺资料 → waiting_manual（0 child）
        outcome = await orchestration_service.prepare_orchestration_start(task.task.task_id)
        orchestration_id = outcome.orchestration.orchestration_id
        await _wait_orchestration(
            orchestration_service, orchestration_id, OrchestrationStatus.WAITING_HUMAN.value
        )
        projection = await orchestration_service.get_orchestration(orchestration_id)
        assert projection.current_phase == OrchestrationPhase.WAITING_MANUAL.value

        # ---- 用户上传 PDF（生产 ingestion；news 类禁止，用 annual_report）
        ingestion = SourceIngestionService(sessionmaker, raw_store)
        upload = await ingestion.ingest_upload(
            company_id=UUID(str(company.company_id)),
            provider_key="szse",
            document_type=SourceDocumentType.ANNUAL_REPORT,
            title="宁德时代2023年年度报告",
            source_url="https://www.szse.cn/annual.pdf",
            published_at=date(2024, 4, 30),
            reporting_period_end=date(2023, 12, 31),
            external_document_id=None,
            stream=io.BytesIO(single_page_pdf()),
        )
        source_id = upload.record.source_id
        # 生产 source preparation（parse → chunk → index；幂等）
        prep_result = await source_preparation.prepare_source(source_id)
        assert prep_result.status == "prepared", prep_result
        assert await _count(sessionmaker, "parsed_sources") >= 1
        assert await _count(sessionmaker, "chunk_sets") >= 1
        assert await _count(sessionmaker, "chunk_vector_indexes") >= 1

        # ---- 用户点「继续研究」→ 同 orchestration 同顶层线程 → completed
        resumed = await orchestration_service.resume_after_source_acquisition(orchestration_id)
        assert resumed.orchestration_id == orchestration_id
        await _wait_orchestration(
            orchestration_service, orchestration_id, OrchestrationStatus.COMPLETED.value
        )

        # ---- D. Report / Citation 链
        report_count = await _count(sessionmaker, "reports")
        evidence_count = await _count(sessionmaker, "evidence_cards")
        claim_count = await _count(sessionmaker, "claims")
        link_count = await _count(sessionmaker, "claim_evidence_links")
        assert report_count == 1, report_count
        assert evidence_count >= 1, evidence_count
        assert claim_count >= 1, claim_count
        assert link_count >= 1, link_count
        # citation 链：每张 evidence 卡都引用真实 source（source → evidence → claim）。
        async with sessionmaker() as session:
            orphan_evidence = int(
                (
                    await session.execute(
                        text(
                            "SELECT count(*) FROM evidence_cards "
                            "WHERE origin_type = 'document_chunk' AND source_id IS NULL"
                        )
                    )
                ).scalar_one()
            )
        assert orphan_evidence == 0, orphan_evidence
    finally:
        await manager.close()
        await checkpoint.close()


# ------------------------------------------------------------------ helpers


async def _wait_orchestration(service, orchestration_id: UUID, expected_status: str) -> None:
    """轮询编排投影至目标终态（后台任务由 execution_manager 驱动）。"""
    deadline = time.monotonic() + 180.0
    while time.monotonic() < deadline:
        projection = await service.get_orchestration(orchestration_id)
        if projection.status == expected_status:
            return
        if projection.status in (
            OrchestrationStatus.FAILED.value,
            OrchestrationStatus.CANCELLED.value,
        ):
            raise AssertionError(
                f"orchestration {projection.status}: "
                f"{projection.error_code} {projection.error_message}"
            )
        await asyncio.sleep(1.0)
    raise AssertionError(f"orchestration did not reach {expected_status} in time")
