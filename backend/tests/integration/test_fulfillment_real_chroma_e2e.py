"""ResearchFulfillmentService real PG + real Chroma E2E (stage 7A.2A spec C).

FakeRetrieval / FakeIndexBuilder 不能作 Final Gate → 本测试复用 Stage3 真实链路
ChunkingService → VectorIndexService → RetrievalService（real Chroma + PG
hydrate），仅 FakeEmbeddingProvider / FakeEvidenceExtractionModel /
FakeResearchPlannerModel（**0 real DeepSeek / 0 Web / 0 live provider**）。
集中覆盖：

- **C2 ready-index path**：seed company/task/archived HTML source/parsed/chunkset
  → 先建 ready vector index → Planner→Router→Preparation ready=false →
  Fulfillment 内部真实 RetrievalService → Chroma filtered query → PG hydrate →
  RetrievalHit（**禁止手工构造 hit**）→ EvidenceExtraction → EvidenceCard →
  Preparation ready 提升；断言卡 research_question_sha256 == hash(frozen plan
  question)、provenance 正确、ready 按 plan 提升；
- **C3 no-index path**：source archived+parsed 但无 ready index → 审计确认
  `SourceIndexBuilder` 已是 concrete production adapter（ParsedSource→Chunking→
  VectorIndex，只 archived+parsed、不下载不 live fetch）→ 注入后 executor 自动
  补建 index → 检索 → 证据 → RESOLVED；不注入 → INDEX_NOT_READY（验证审计结论）；
- **C4 隔离**：每次测试独立 collection（`test_fulfillment_<uuid>`），finally
  删除，PG 用 `_cleanup` 清空，不污染其它测试 / 共享 collection。
"""

import math
from datetime import date
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.evidence.contracts import (
    EvidenceConfidence,
    EvidenceType,
    compute_research_question_sha256,
)
from app.evidence.extractor.contracts import (
    EvidenceExtractionDecision,
    EvidenceExtractionItem,
    EvidenceExtractionReason,
)
from app.rag.embedding.contracts import EmbeddingModelSpec
from app.rag.index.service import VectorIndexService
from app.rag.retrieval.service import RetrievalService
from app.research_fulfillment.contracts import (
    FulfillmentErrorCode,
    FulfillmentStatus,
)
from app.research_fulfillment.executors import (
    DocumentNeedExecutor,
    FinancialNeedExecutor,
    MacroNeedExecutor,
    ValuationNeedExecutor,
)
from app.research_fulfillment.executors.document import SourceIndexBuilder
from app.research_fulfillment.service import ResearchFulfillmentService
from app.research_planning.preparation import (
    MissingReasonCode,
    ResearchPreparationService,
)
from app.research_planning.router import ResearchSourceRouter
from app.research_planning.service import ResearchPlanningService
from app.services.chunking_service import ChunkingService
from app.services.company_identity_service import CompanyIdentityService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from app.vectorstore.client import ChromaManager
from tests.embedding.fakes import FakeEmbeddingProvider
from tests.evidence.fakes import FakeEvidenceExtractionModel
from tests.integration.research_fulfillment_helpers import _unique_quote
from tests.integration.test_evidence_card_service import _seed_html_source
from tests.integration.test_research_planning_service import (
    _cleanup,
    _plan_payload,
    _seed_company,
    _seed_research_task,
)
from tests.research_planning.fakes import FakeResearchPlannerModel

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_Q1 = "分析贵州茅台的经营质量、主要风险和估值水平。"
_D1 = date(2026, 8, 10)

# 只含 document need 的 payload：business module 输入 = 文档证据池；无
# financial/macro/event/valuation need → 无其它 missing 干扰 ready 断言。
_DOC_ONLY = dict(
    document_needs=[
        {"need_code": "news_docs", "purpose": "需要公司新闻", "source_type": "news_article"}
    ],
    financial_needs=[],
    macro_needs=[],
    event_needs=[],
    valuation_needs=[],
    analysis_modules=["business_event"],
    research_scope=["business"],
)

_TEST_SPEC = EmbeddingModelSpec(
    model_id="BAAI/bge-small-zh-v1.5",
    dimension=512,
    normalize_embeddings=True,
    query_instruction="为这个句子生成表示以用于检索相关文章：",
    max_input_tokens=512,
    revision="test-revision-001",
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
    task_id = await _seed_research_task(sessionmaker, questions=[_Q1], end_date=_D1)
    yield {
        "sessionmaker": sessionmaker,
        "raw_store": raw_store,
        "company_id": company_id,
        "task_id": task_id,
    }
    await _cleanup(sessionmaker)


# ---------------------------------------------------------------- helpers


async def _drop_collection(client, collection_name: str) -> None:
    """C4：删除独立测试 collection；缺失不掩盖真实断言。"""
    try:
        await client.delete_collection(collection_name)
    except Exception:
        pass


def _decision_for_text(text: str) -> EvidenceExtractionDecision:
    """按真实 hit.text 生成确定性 decision（quote 在该 chunk 内唯一可解析）。

    `_seed_html_source` 的 `_MULTI_HTML` 会切出**全同字符** chunk（如
    `戊`*200，无内部字符边界）——这类文本不存在唯一子串，返回 relevant=False
    （no evidence），避免 EvidenceExtractionQuoteAmbiguous。含段落交界的 chunk
    由 `_unique_quote` 取跨边界窗口（唯一）→ 产出 evidence 卡。
    """
    if not any(text[i] != text[i - 1] for i in range(1, len(text))):
        return EvidenceExtractionDecision(
            relevant=False, items=[], reason_code=EvidenceExtractionReason.NOT_RELEVANT
        )
    return EvidenceExtractionDecision(
        relevant=True,
        items=[
            EvidenceExtractionItem(
                evidence_statement="贵州茅台发布经营相关新闻。",
                evidence_type=EvidenceType.METRIC,
                quote_text=_unique_quote(text, 20),
                confidence=EvidenceConfidence.HIGH,
            )
        ],
    )


class _PerHitExtractionModel(FakeEvidenceExtractionModel):
    """对每个真实 RetrievalHit 按其文本生成确定性 decision（多 hit 场景）。

    record (research_question, RetrievalHit) 到 .calls —— hits 全部来自真实
    检索链（Chroma query + PG hydrate），**不在测试中手工构造**。
    """

    async def extract(self, research_question: str, retrieval_hit):
        self.calls.append((research_question, retrieval_hit))
        return _decision_for_text(retrieval_hit.text)


def _build_fulfillment(
    env: dict,
    chroma_manager: ChromaManager,
    *,
    collection_name: str,
    extractor,
    index_builder=None,
):
    """组装带真实检索链的 ResearchFulfillmentService + 其依赖（供测试分段调用）。"""
    plan_service = ResearchPlanningService(
        env["sessionmaker"],
        FakeResearchPlannerModel(_plan_payload(**_DOC_ONLY)),
        CompanyIdentityService(env["sessionmaker"]),
    )
    router = ResearchSourceRouter(env["sessionmaker"], plan_service)
    preparation = ResearchPreparationService(env["sessionmaker"], plan_service, router)
    retrieval = RetrievalService(
        sessionmaker=env["sessionmaker"],
        embedding_provider=FakeEmbeddingProvider(_TEST_SPEC),
        chroma=chroma_manager,
        collection_name=collection_name,
    )
    service = ResearchFulfillmentService(
        env["sessionmaker"],
        plan_service,
        router,
        preparation,
        document_executor=DocumentNeedExecutor(
            env["sessionmaker"], retrieval, extractor, index_builder=index_builder
        ),
        financial_executor=FinancialNeedExecutor(env["sessionmaker"]),
        macro_executor=MacroNeedExecutor(env["sessionmaker"]),
        valuation_executor=ValuationNeedExecutor(),
    )
    return service, plan_service, router, preparation


# ================================================================ C2 ready-index path


async def test_ready_index_path_real_retrieval_chain(env, chroma_manager) -> None:
    """C2：ready vector index → Fulfillment 内部真实检索链 → 卡 hash(Q1) + ready 提升。"""
    src, _, chunk_set_id, chunks = await _seed_html_source(env)
    collection_name = f"test_fulfillment_{uuid4().hex[:12]}"
    embedding = FakeEmbeddingProvider(_TEST_SPEC)
    index_service = VectorIndexService(
        sessionmaker=env["sessionmaker"],
        embedding_provider=embedding,
        chroma=chroma_manager,
        collection_name=collection_name,
    )
    indexed = await index_service.index_chunk_set(chunk_set_id)
    assert indexed.status == "ready"

    extractor = _PerHitExtractionModel()
    client = await chroma_manager.get_client()
    try:
        service, plan_service, router, preparation = _build_fulfillment(
            env,
            chroma_manager,
            collection_name=collection_name,
            extractor=extractor,
        )
        plan = await plan_service.create_plan(env["task_id"])
        await router.route_research_plan(plan.research_plan_id)

        # 前置：无证据卡 → ready=false（source 存在但无已提取 evidence）。
        before = await preparation.prepare_research(plan.research_plan_id)
        assert before.ready_for_analysis is False
        news_missing = next(n for n in before.missing_needs if n.need_code == "news_docs")
        assert news_missing.reason_code == MissingReasonCode.INSUFFICIENT_EVIDENCE

        outcome = await service.fulfill_research_needs(plan.research_plan_id)
        news = {a.need_code: a for a in outcome.attempts}["news_docs"]
        assert news.status == FulfillmentStatus.RESOLVED
        assert news.created_artifact_ids, "真实检索链必须创建新证据卡"
        assert not news.existing_artifact_ids

        # 禁止手工构造 RetrievalHit：extractor 收到真实检索链的 hits。
        assert extractor.calls, "必须发生真实检索抽取"
        for question, hit in extractor.calls:
            assert question == _Q1
            assert hit.source_id == src
            assert hit.company_id == env["company_id"]
            assert hit.chunk_set_id == chunk_set_id
            assert hit.text in {c.text for c in chunks}  # 真实 PG hydrate 正文
            assert math.isfinite(hit.distance)

        # 新卡 research_question_sha256 == hash(frozen plan question)、provenance 正确。
        card_id = news.created_artifact_ids[0]
        async with env["sessionmaker"]() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT research_question_sha256, chunk_id, source_id, company_id "
                        "FROM evidence_cards WHERE evidence_card_id = :cid"
                    ).bindparams(cid=card_id)
                )
            ).one()
        assert row[0] == compute_research_question_sha256(_Q1)
        # 卡挂在真实检索命中 chunk 上（命中来自 seed chunk，非手工构造）。
        hit_chunk_ids = {hit.chunk_id for _, hit in extractor.calls}
        assert row[1] in hit_chunk_ids
        assert any(c.chunk_id == row[1] for c in chunks)
        assert row[2] == src
        assert row[3] == env["company_id"]

        # ready 按 plan 提升；stage4 request 用 frozen Q1/D1。
        assert outcome.ready_for_analysis is True
        assert outcome.stage4_request["research_question"] == _Q1
        assert outcome.stage4_request["analysis_as_of"] == _D1.isoformat()
        after = await preparation.prepare_research(plan.research_plan_id)
        assert after.ready_for_analysis is True
        assert after.missing_needs == ()
    finally:
        await _drop_collection(client, collection_name)


# ================================================================ C3 no-index path


async def test_no_index_path_source_index_builder_builds_ready_index(env, chroma_manager) -> None:
    """C3：source archived+parsed 但无 ready index → SourceIndexBuilder 自动补建 → RESOLVED。

    审计结论：`SourceIndexBuilder` 已是 concrete production adapter（只对
    archived+parsed source 走 ChunkingService + VectorIndexService，不下载不
    live fetch）。此处注入真实实例验证补建真的发生（manifest → ready）。
    """
    src, _, chunk_set_id, _ = await _seed_html_source(env)
    collection_name = f"test_fulfillment_{uuid4().hex[:12]}"
    embedding = FakeEmbeddingProvider(_TEST_SPEC)
    index_builder = SourceIndexBuilder(
        env["sessionmaker"],
        ChunkingService(env["sessionmaker"]),
        VectorIndexService(
            sessionmaker=env["sessionmaker"],
            embedding_provider=embedding,
            chroma=chroma_manager,
            collection_name=collection_name,
        ),
    )
    extractor = _PerHitExtractionModel()
    client = await chroma_manager.get_client()
    try:
        service, plan_service, router, preparation = _build_fulfillment(
            env,
            chroma_manager,
            collection_name=collection_name,
            extractor=extractor,
            index_builder=index_builder,
        )
        plan = await plan_service.create_plan(env["task_id"])
        await router.route_research_plan(plan.research_plan_id)

        outcome = await service.fulfill_research_needs(plan.research_plan_id)
        news = {a.need_code: a for a in outcome.attempts}["news_docs"]
        assert news.status == FulfillmentStatus.RESOLVED
        assert news.created_artifact_ids, "SourceIndexBuilder 补建后必须检索出证据"
        assert extractor.calls
        assert outcome.ready_for_analysis is True

        # 补建真的发生了：该独立 collection 的 manifest 现在 ready。
        async with env["sessionmaker"]() as session:
            status = (
                await session.execute(
                    text(
                        "SELECT status FROM chunk_vector_indexes WHERE collection_name = :c"
                    ).bindparams(c=collection_name)
                )
            ).scalar_one()
        assert status == "ready"
    finally:
        await _drop_collection(client, collection_name)


async def test_no_index_without_builder_reports_index_not_ready(env, chroma_manager) -> None:
    """C3 审计结论验证：不注入 SourceIndexBuilder → source 存在但无 ready index → INDEX_NOT_READY。

    证明补建能力来自 `SourceIndexBuilder` 注入，而非 executor 默认行为。
    """
    await _seed_html_source(env)
    collection_name = f"test_fulfillment_{uuid4().hex[:12]}"
    client = await chroma_manager.get_client()
    try:
        service, plan_service, router, _ = _build_fulfillment(
            env,
            chroma_manager,
            collection_name=collection_name,
            extractor=_PerHitExtractionModel(),
        )
        plan = await plan_service.create_plan(env["task_id"])
        await router.route_research_plan(plan.research_plan_id)

        outcome = await service.fulfill_research_needs(plan.research_plan_id)
        news = {a.need_code: a for a in outcome.attempts}["news_docs"]
        assert news.status == FulfillmentStatus.UNRESOLVED
        assert news.error_code == FulfillmentErrorCode.INDEX_NOT_READY
        assert outcome.ready_for_analysis is False
    finally:
        await _drop_collection(client, collection_name)
