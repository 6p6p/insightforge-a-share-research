"""ResearchBackflowExecutor real PG + real Chroma E2E (stage 7A.2B.3 spec K-X).

FakeRetrieval 不能作 Final Gate → 复用 Stage3 真实链路 ChunkingService →
VectorIndexService → RetrievalService（real Chroma + PG hydrate），仅
FakeEmbeddingProvider / FakeEvidenceExtractionModel（**0 real DeepSeek / 0 Web /
0 live provider**）。覆盖：

- **ready-index path**：seed 官方披露 source → ready index → plan need_spec →
  execute → resolved，created EvidenceCard hash(source synthesis question)；
- **replay idempotent**：同 plan 再执行 → fingerprint replay → replayed，无新卡；
- **no source**：company 无匹配 document_type → manual_required(
  source_acquisition_required)（不假装完成）；
- **no index without builder**：source 存在但无 ready index 且未注入
  SourceIndexBuilder → manual_required(index_not_ready)；
- **weak_source_quality 过滤**：allowed_source_types 只官方披露（无
  news_article）→ 仅 seed news → manual_required(source_acquisition_required)。

隔离：每次测试独立 collection（`test_backflow_<uuid>`），finally 删除，PG 用
`_cleanup` 清空。`VerifiedResearchBackflowRequest` 为最小占位（executor 只读
company_id / analysis_as_of / verified_source_synthesis.research_question）。
"""

import math
from datetime import date, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.analysis.synthesis.contracts import VerifiedSynthesisResult
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.domain.source_records import SourceDocumentType
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
from app.report.contracts import VerifiedReport
from app.research_backflow.contracts import (
    MAX_QUERIES_PER_NEED,
    RESEARCH_BACKFLOW_MANUAL_REASON_EVIDENCE_NOT_EXTRACTED,
    RESEARCH_BACKFLOW_MANUAL_REASON_INDEX_NOT_READY,
    RESEARCH_BACKFLOW_MANUAL_REASON_SOURCE_ACQUISITION,
    RESEARCH_BACKFLOW_NEED_STATUS_MANUAL_REQUIRED,
    RESEARCH_BACKFLOW_NEED_STATUS_RESOLVED,
    VerifiedResearchBackflowRequest,
)
from app.research_backflow.executor import ResearchBackflowExecutor
from app.review.contracts import VerifiedReviewAction
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from app.vectorstore.client import ChromaManager
from tests.embedding.fakes import FakeEmbeddingProvider
from tests.evidence.fakes import FakeEvidenceExtractionModel
from tests.integration.research_fulfillment_helpers import _unique_quote
from tests.integration.test_evidence_card_service import _seed_html_source
from tests.integration.test_research_planning_service import (
    _cleanup,
    _seed_company,
    _seed_research_task,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_Q1 = "分析贵州茅台的经营质量、主要风险和估值水平。"
_D1 = date(2026, 8, 10)

# weak_source_quality need 的 allowed_source_types（官方披露，无 news_article）。
_OFFICIAL_DOCUMENT_TYPES = [
    SourceDocumentType.ANNUAL_REPORT.value,
    SourceDocumentType.SEMIANNUAL_REPORT.value,
    SourceDocumentType.QUARTERLY_REPORT.value,
    SourceDocumentType.COMPANY_ANNOUNCEMENT.value,
    SourceDocumentType.ISSUER_IR_MATERIAL.value,
    SourceDocumentType.PROSPECTUS.value,
]

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
    """隔离：删除独立测试 collection；缺失不掩盖真实断言。"""
    try:
        await client.delete_collection(collection_name)
    except Exception:
        pass


def _decision_for_text(text: str) -> EvidenceExtractionDecision:
    """按真实 hit.text 生成确定性 decision（quote 在该 chunk 内唯一可解析）。

    `_seed_html_source` 的 `_MULTI_HTML` 会切出**全同字符** chunk（无内部字符
    边界）→ 不存在唯一子串，返回 relevant=False（no evidence）。含段落交界的
    chunk 由 `_unique_quote` 取跨边界窗口（唯一）→ 产出 evidence 卡。
    """
    if not any(text[i] != text[i - 1] for i in range(1, len(text))):
        return EvidenceExtractionDecision(
            relevant=False, items=[], reason_code=EvidenceExtractionReason.NOT_RELEVANT
        )
    return EvidenceExtractionDecision(
        relevant=True,
        items=[
            EvidenceExtractionItem(
                evidence_statement="贵州茅台发布经营相关披露材料。",
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


class _NoEvidenceModel(FakeEvidenceExtractionModel):
    """有 ready index + 真实检索 hits，但抽取 0 证据（relevant=False）。"""

    async def extract(self, research_question: str, retrieval_hit):
        return EvidenceExtractionDecision(
            relevant=False, items=[], reason_code=EvidenceExtractionReason.NOT_RELEVANT
        )


def _plan_payload(
    *,
    need_code="unsupported_by_evidence",
    allowed=("annual_report",),
    queries=(_Q1,),
) -> dict:
    """最小 plan_payload（derive 单独测试；此处只喂 executor 需要的 need_spec）。"""
    return {
        "max_queries_per_need": MAX_QUERIES_PER_NEED,
        "need_specs": [
            {
                "need_code": need_code,
                "target_section_ids": ["S1"],
                "related_claim_ids": [],
                "related_evidence_card_ids": [],
                "retrieval_queries": list(queries),
                "allowed_source_types": list(allowed),
            }
        ],
    }


def _verified_request(env: dict) -> VerifiedResearchBackflowRequest:
    """最小 VerifiedResearchBackflowRequest（executor 只读 company_id /
    analysis_as_of / verified_source_synthesis.research_question；其余占位）。"""
    company_id = env["company_id"]
    rq_sha = compute_research_question_sha256(_Q1)
    synthesis = object.__new__(VerifiedSynthesisResult)
    for _field, _value in {
        "synthesis_result_id": uuid4(),
        "synthesis_id": uuid4(),
        "company_id": company_id,
        "research_question": _Q1,
        "research_question_sha256": rq_sha,
        "analysis_as_of": _D1,
        "synthesis_fingerprint": "0" * 64,
        "result_fingerprint": "0" * 64,
        "input_claim_ids": (),
        "alias_map": {},
        "output": None,
    }.items():
        object.__setattr__(synthesis, _field, _value)
    return VerifiedResearchBackflowRequest(
        research_request_id=uuid4(),
        source_stage5_run_id=uuid4(),
        review_action_id=uuid4(),
        human_decision_id=None,
        source_report_id=uuid4(),
        company_id=company_id,
        research_question_sha256=rq_sha,
        analysis_as_of=_D1,
        request_schema_version=1,
        request_payload={},
        request_fingerprint="1" * 64,
        created_at=datetime(2026, 8, 12, 9, 0, 0),
        verified_action=object.__new__(VerifiedReviewAction),
        verified_decision=None,
        verified_report=object.__new__(VerifiedReport),
        verified_source_synthesis=synthesis,
    )


def _build_executor(
    env: dict,
    chroma_manager: ChromaManager,
    *,
    collection_name: str,
    extractor,
    index_builder=None,
) -> ResearchBackflowExecutor:
    """组装带真实检索链的 ResearchBackflowExecutor（供测试分段调用）。"""
    retrieval = RetrievalService(
        sessionmaker=env["sessionmaker"],
        embedding_provider=FakeEmbeddingProvider(_TEST_SPEC),
        chroma=chroma_manager,
        collection_name=collection_name,
    )
    return ResearchBackflowExecutor(
        env["sessionmaker"], retrieval, extractor, index_builder=index_builder
    )


def _assert_real_hits(extractor, src: UUID, chunk_set_id: UUID, env: dict, chunks) -> None:
    """禁止手工构造 RetrievalHit：extractor 收到真实检索链的 hits。"""
    assert extractor.calls, "必须发生真实检索抽取"
    for question, hit in extractor.calls:
        assert question == _Q1
        assert hit.source_id == src
        assert hit.company_id == env["company_id"]
        assert hit.chunk_set_id == chunk_set_id
        assert hit.text in {c.text for c in chunks}  # 真实 PG hydrate 正文
        assert math.isfinite(hit.distance)


# ================================================================ ready-index path


async def test_ready_index_path_creates_evidence(env, chroma_manager) -> None:
    """ready vector index → 真实检索链 → resolved，created EvidenceCard hash(Q1)。"""
    src, _, chunk_set_id, chunks = await _seed_html_source(env, document_type="annual_report")
    collection_name = f"test_backflow_{uuid4().hex[:12]}"
    embedding = FakeEmbeddingProvider(_TEST_SPEC)
    indexed = await VectorIndexService(
        sessionmaker=env["sessionmaker"],
        embedding_provider=embedding,
        chroma=chroma_manager,
        collection_name=collection_name,
    ).index_chunk_set(chunk_set_id)
    assert indexed.status == "ready"

    extractor = _PerHitExtractionModel()
    client = await chroma_manager.get_client()
    try:
        executor = _build_executor(
            env,
            chroma_manager,
            collection_name=collection_name,
            extractor=extractor,
        )
        result = await executor.execute_supplemental_research(
            _verified_request(env), _plan_payload(allowed=["annual_report"])
        )
        assert result.all_manual_required is False
        assert result.resolved_need_codes == ("unsupported_by_evidence",)
        # executor 的 new_evidence_card_ids 按 str 排序（确定性）；attempt 的
        # created 是创建顺序。单卡时一致，多卡时仅顺序不同 → set 比较。
        assert set(result.new_evidence_card_ids) == set(
            result.attempts[0].created_evidence_card_ids
        )
        need = result.attempts[0]
        assert need.status == RESEARCH_BACKFLOW_NEED_STATUS_RESOLVED
        assert need.manual_required_reason is None
        assert need.created_evidence_card_ids, "真实检索链必须创建新证据卡"
        assert need.replayed_evidence_card_ids == ()

        _assert_real_hits(extractor, src, chunk_set_id, env, chunks)

        # 新卡 research_question_sha256 == hash(source synthesis question)、挂真实命中 chunk。
        card_id = need.created_evidence_card_ids[0]
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
        hit_chunk_ids = {hit.chunk_id for _, hit in extractor.calls}
        assert row[1] in hit_chunk_ids
        assert any(c.chunk_id == row[1] for c in chunks)
        assert row[2] == src
        assert row[3] == env["company_id"]
    finally:
        await _drop_collection(client, collection_name)


# ================================================================ replay idempotent


async def test_replay_reuses_evidence_cards(env, chroma_manager) -> None:
    """同 plan 再执行 → fingerprint replay → replayed，无新卡（幂等）。"""
    src, _, chunk_set_id, chunks = await _seed_html_source(env, document_type="annual_report")
    collection_name = f"test_backflow_{uuid4().hex[:12]}"
    await VectorIndexService(
        sessionmaker=env["sessionmaker"],
        embedding_provider=FakeEmbeddingProvider(_TEST_SPEC),
        chroma=chroma_manager,
        collection_name=collection_name,
    ).index_chunk_set(chunk_set_id)

    extractor = _PerHitExtractionModel()
    client = await chroma_manager.get_client()
    try:
        executor = _build_executor(
            env,
            chroma_manager,
            collection_name=collection_name,
            extractor=extractor,
        )
        payload = _plan_payload(allowed=["annual_report"])
        first = await executor.execute_supplemental_research(_verified_request(env), payload)
        second = await executor.execute_supplemental_research(_verified_request(env), payload)

        assert first.attempts[0].status == RESEARCH_BACKFLOW_NEED_STATUS_RESOLVED
        assert first.attempts[0].created_evidence_card_ids
        assert first.attempts[0].replayed_evidence_card_ids == ()

        assert second.attempts[0].status == RESEARCH_BACKFLOW_NEED_STATUS_RESOLVED
        assert second.attempts[0].created_evidence_card_ids == ()
        assert set(second.attempts[0].replayed_evidence_card_ids) == set(
            first.attempts[0].created_evidence_card_ids
        )
        # 执行摘要只投影 application output。
        assert second.new_evidence_card_ids == ()
    finally:
        await _drop_collection(client, collection_name)


# ================================================================ manual_required


async def test_no_matching_source_manual_required(env, chroma_manager) -> None:
    """company 无匹配 document_type → manual_required(source_acquisition_required)。"""
    collection_name = f"test_backflow_{uuid4().hex[:12]}"
    client = await chroma_manager.get_client()
    try:
        executor = _build_executor(
            env,
            chroma_manager,
            collection_name=collection_name,
            extractor=_PerHitExtractionModel(),
        )
        result = await executor.execute_supplemental_research(
            _verified_request(env), _plan_payload(allowed=["annual_report"])
        )
        assert result.all_manual_required is True
        assert result.resolved_need_codes == ()
        need = result.attempts[0]
        assert need.status == RESEARCH_BACKFLOW_NEED_STATUS_MANUAL_REQUIRED
        assert need.manual_required_reason == RESEARCH_BACKFLOW_MANUAL_REASON_SOURCE_ACQUISITION
        assert need.created_evidence_card_ids == ()
    finally:
        await _drop_collection(client, collection_name)


async def test_no_index_without_builder_manual_required(env, chroma_manager) -> None:
    """source 存在但无 ready index 且未注入 SourceIndexBuilder → index_not_ready。"""
    await _seed_html_source(env, document_type="annual_report")
    collection_name = f"test_backflow_{uuid4().hex[:12]}"
    client = await chroma_manager.get_client()
    try:
        executor = _build_executor(
            env,
            chroma_manager,
            collection_name=collection_name,
            extractor=_PerHitExtractionModel(),
            index_builder=None,
        )
        result = await executor.execute_supplemental_research(
            _verified_request(env), _plan_payload(allowed=["annual_report"])
        )
        need = result.attempts[0]
        assert need.status == RESEARCH_BACKFLOW_NEED_STATUS_MANUAL_REQUIRED
        assert need.manual_required_reason == RESEARCH_BACKFLOW_MANUAL_REASON_INDEX_NOT_READY
        assert result.all_manual_required is True
    finally:
        await _drop_collection(client, collection_name)


async def test_weak_source_quality_ignores_news(env, chroma_manager) -> None:
    """weak_source_quality allowed 只官方披露（无 news）→ 仅 seed news → 不研究。"""
    await _seed_html_source(env, document_type="news_article")
    collection_name = f"test_backflow_{uuid4().hex[:12]}"
    client = await chroma_manager.get_client()
    try:
        executor = _build_executor(
            env,
            chroma_manager,
            collection_name=collection_name,
            extractor=_PerHitExtractionModel(),
        )
        result = await executor.execute_supplemental_research(
            _verified_request(env),
            _plan_payload(need_code="weak_source_quality", allowed=_OFFICIAL_DOCUMENT_TYPES),
        )
        need = result.attempts[0]
        assert need.status == RESEARCH_BACKFLOW_NEED_STATUS_MANUAL_REQUIRED
        assert need.manual_required_reason == RESEARCH_BACKFLOW_MANUAL_REASON_SOURCE_ACQUISITION
        # 未被 news_article 带偏：只有官方类型 allowed，news 不属于官方披露。
        assert "news_article" not in _OFFICIAL_DOCUMENT_TYPES
    finally:
        await _drop_collection(client, collection_name)


async def test_index_ready_but_no_evidence_manual_required(env, chroma_manager) -> None:
    """有 index + 真实检索 hits 但抽取 0 证据 → manual_required(evidence_not_extracted)。"""
    _, _, chunk_set_id, _ = await _seed_html_source(env, document_type="annual_report")
    collection_name = f"test_backflow_{uuid4().hex[:12]}"
    await VectorIndexService(
        sessionmaker=env["sessionmaker"],
        embedding_provider=FakeEmbeddingProvider(_TEST_SPEC),
        chroma=chroma_manager,
        collection_name=collection_name,
    ).index_chunk_set(chunk_set_id)

    client = await chroma_manager.get_client()
    try:
        executor = _build_executor(
            env,
            chroma_manager,
            collection_name=collection_name,
            extractor=_NoEvidenceModel(),
        )
        result = await executor.execute_supplemental_research(
            _verified_request(env), _plan_payload(allowed=["annual_report"])
        )
        need = result.attempts[0]
        assert need.status == RESEARCH_BACKFLOW_NEED_STATUS_MANUAL_REQUIRED
        assert need.manual_required_reason == RESEARCH_BACKFLOW_MANUAL_REASON_EVIDENCE_NOT_EXTRACTED
        assert need.created_evidence_card_ids == ()
        assert result.all_manual_required is True
    finally:
        await _drop_collection(client, collection_name)
