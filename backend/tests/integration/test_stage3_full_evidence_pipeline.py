"""Stage 3 full-chain E2E acceptance (Gate 0).

单条真实贯穿链（零真实网络 / 零真实 LLM）：

  Source → ParsedSource → DocumentChunk → VectorIndexService → RetrievalService
  → RetrievalHit → EvidenceExtractionService → EvidenceCard

- 需要真实 PostgreSQL（127.0.0.1:5433）+ 真实 Chroma（127.0.0.1:8002）。
- Embedding 用 FakeEmbeddingProvider（确定性），Extractor 用
  FakeEvidenceExtractionModel，**RetrievalHit 全部由 RetrievalService 经真实
  Chroma + PG hydrate 产生，不允许手工构造**；**DocumentChunk 全部由
  ChunkingService 产生，不允许手工 seed**。
- 允许只 seed：Company / Provider / 原始合法 Source 前置。

覆盖：
1. HTML：SourceRecord → SourceParsingService → ChunkingService →
   VectorIndexService → RetrievalService → EvidenceExtractionService →
   EvidenceCard → chunk → ParsedSource → SourceRecord → RawArtifact → DOM
   locator（跨 block 2 refs）；
2. PDF：SourceRecord（真实 SourceIngestionService 上传）→ 真实解析
   （pdf_layout v2）→ ChunkingService → index → retrieve → extract →
   EvidenceCard → page/bbox locator（跨页 2 refs）→ RawArtifact。

Macro 不走 Vector Retrieval（现有 Macro Evidence E2E 保留）。
"""

import hashlib
import io
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.company import CompanyModel
from app.db.session import DatabaseManager
from app.domain.source_records import SourceDocumentType
from app.evidence.contracts import EvidenceConfidence, EvidenceType
from app.evidence.extractor.contracts import (
    EVIDENCE_EXTRACTOR_NAME,
    EVIDENCE_EXTRACTOR_VERSION,
    EvidenceExtractionDecision,
    EvidenceExtractionItem,
)
from app.evidence.extractor.service import EvidenceExtractionService
from app.rag.embedding.contracts import EmbeddingModelSpec
from app.rag.index.service import VectorIndexService
from app.rag.retrieval.contracts import RetrievalQuery
from app.rag.retrieval.service import RetrievalService
from app.repositories.company_repository import CompanyRepository
from app.repositories.document_chunk_repository import DocumentChunkRepository
from app.repositories.evidence_card_repository import EvidenceCardRepository
from app.services.chunking_service import ChunkingService
from app.services.source_ingestion_service import SourceIngestionService
from app.services.source_parsing_service import SourceParsingService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from app.vectorstore.client import ChromaManager
from tests.embedding.fakes import FakeEmbeddingProvider
from tests.evidence.fakes import FakeEvidenceExtractionModel
from tests.integration.test_evidence_card_service import (
    _seed_html_source,
    _sha,
)
from tests.pdf_fixtures import duplicate_line_across_pages_pdf

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_PUBLISHED_AT = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)

_TEST_SPEC = EmbeddingModelSpec(
    model_id="BAAI/bge-small-zh-v1.5",
    dimension=512,
    normalize_embeddings=True,
    query_instruction="为这个句子生成表示以用于检索相关文章：",
    max_input_tokens=512,
    revision="test-revision-001",
)

_QUESTION = "2024年贵州茅台净利润增长情况？"
_STATEMENT = "贵州茅台2024年归属净利润同比增长15%。"


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


async def _cleanup(sessionmaker) -> None:
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM evidence_cards"))
        await session.execute(text("DELETE FROM chunk_vector_indexes"))
        await session.execute(text("DELETE FROM document_chunks"))
        await session.execute(text("DELETE FROM chunk_sets"))
        await session.execute(text("DELETE FROM parsed_source_blocks"))
        await session.execute(text("DELETE FROM parsed_sources"))
        await session.execute(text("DELETE FROM news_source_verifications"))
        await session.execute(text("DELETE FROM news_discovery_candidates"))
        await session.execute(text("DELETE FROM news_discovery_runs"))
        await session.execute(text("DELETE FROM source_records"))
        await session.execute(text("DELETE FROM raw_artifacts"))
        await session.execute(text("DELETE FROM company_aliases"))
        await session.execute(text("DELETE FROM companies"))
        await session.commit()


@pytest_asyncio.fixture
async def env(tmp_path, sessionmaker) -> dict:
    raw_store = LocalRawArtifactStore(root=tmp_path / "raw", max_bytes=1024 * 1024)
    await _cleanup(sessionmaker)
    await SourceRegistryService(sessionmaker).seed_defaults()
    company_id = uuid4()
    async with sessionmaker() as session:
        await CompanyRepository(session).create(
            CompanyModel(
                company_id=company_id,
                exchange="SSE",
                security_code="600519",
                identity_key="SSE:600519",
                board="sse_main",
                official_name="测试公司",
                short_name="测试",
                listing_status="listed",
                identity_source_provider_key="sse",
                identity_source_url="https://www.sse.com.cn",
            )
        )
        await session.commit()
    yield {
        "sessionmaker": sessionmaker,
        "raw_store": raw_store,
        "company_id": company_id,
    }
    await _cleanup(sessionmaker)


# ---------------------------------------------------------------- 服务构造


def _index_retrieval(env: dict, chroma_manager, collection_name: str) -> tuple:
    index = VectorIndexService(
        sessionmaker=env["sessionmaker"],
        embedding_provider=FakeEmbeddingProvider(_TEST_SPEC),
        chroma=chroma_manager,
        collection_name=collection_name,
    )
    retrieval = RetrievalService(
        sessionmaker=env["sessionmaker"],
        embedding_provider=FakeEmbeddingProvider(_TEST_SPEC),
        chroma=chroma_manager,
        collection_name=collection_name,
    )
    return index, retrieval


def _hit_by_chunk_id(hits, chunk_id) -> object:
    for hit in hits:
        if hit.chunk_id == chunk_id:
            return hit
    raise AssertionError(f"no retrieval hit for chunk {chunk_id}")


async def _card_by_id(sessionmaker, card_id):
    async with sessionmaker() as session:
        return await EvidenceCardRepository(session).get_by_id(card_id)


# ---------------------------------------------------------------- HTML 全链


async def test_html_full_pipeline_source_to_evidence_card(env, chroma_manager) -> None:
    """HTML：SourceRecord → parse → chunk → index → retrieve → extract → EvidenceCard。

    验证：
    - RetrievalHit 全部来自真实 RetrievalService（不手工构造）；
    - EvidenceCard quote 精确切片 + sha256；
    - EvidenceCard → chunk → ParsedSource → SourceRecord → RawArtifact 完整回溯；
    - DOM locator（跨 block 2 refs，xpath 前缀正确）。
    """
    src, parsed_id, cs_id, chunks = await _seed_html_source(env)
    chunk = chunks[0]  # "甲"*200 + "\n" + "乙"*200（401 字）
    quote = chunk.text[195:211]  # 唯一（跨 "\n"）

    collection_name = f"test_fullchain_html_{uuid4().hex[:12]}"
    index, retrieval = _index_retrieval(env, chroma_manager, collection_name)
    client = await chroma_manager.get_client()
    try:
        result = await index.index_chunk_set(cs_id)
        assert result.status == "ready"
        assert result.indexed_chunk_count == len(chunks)

        hits = await retrieval.retrieve(
            RetrievalQuery(company_id=env["company_id"], query_text="净利润增长", top_k=5)
        )
        assert hits  # 真实 Chroma 返回候选
        hit = _hit_by_chunk_id(hits, chunk.chunk_id)
        assert hit.text == chunk.text  # PG hydrate 正文一致
        assert hit.source_id == src
        assert hit.parsed_source_id == parsed_id
        assert hit.company_id == env["company_id"]
    finally:
        await client.delete_collection(collection_name)

    # 以真实 RetrievalHit 走 EvidenceExtractionService（Fake 模型，零真实 LLM）。
    fake = FakeEvidenceExtractionModel(
        decision=EvidenceExtractionDecision(
            relevant=True,
            items=[
                EvidenceExtractionItem(
                    evidence_statement=_STATEMENT,
                    evidence_type=EvidenceType.METRIC,
                    quote_text=quote,
                    confidence=EvidenceConfidence.HIGH,
                )
            ],
        )
    )
    service = EvidenceExtractionService(env["sessionmaker"], model=fake)
    extracted = await service.extract_from_hit(_QUESTION, hit)
    assert extracted.relevant is True
    assert extracted.created_count == 1
    assert extracted.replayed_count == 0
    assert len(extracted.evidence_card_ids) == 1
    assert len(fake.calls) == 1  # LLM 侧只有 fake（零真实 provider / 零网络）

    card = await _card_by_id(env["sessionmaker"], extracted.evidence_card_ids[0])
    assert card is not None
    assert card.origin_type == "document_chunk"
    assert card.quote_text == quote
    assert card.quote_sha256 == _sha(quote)
    assert card.quote_start == 195
    assert card.quote_end == 211
    assert card.extractor_name == EVIDENCE_EXTRACTOR_NAME
    assert card.extractor_version == EVIDENCE_EXTRACTOR_VERSION
    assert card.extractor_model_id == fake.model_id
    assert card.extractor_confidence == "high"

    # quote 级 DOM locator：跨 block → 2 个。
    assert len(card.locator_refs) == 2
    assert card.locator_refs[0]["locator"]["type"] == "html_dom"
    assert card.locator_refs[0]["locator"]["xpath"].startswith("/html/body/article/p[")
    assert card.locator_refs[1]["locator"]["type"] == "html_dom"
    assert card.locator_refs[1]["char_start"] == 0

    # 完整回溯：EvidenceCard → chunk → ParsedSource → SourceRecord → RawArtifact。
    async with env["sessionmaker"]() as session:
        trace = await session.execute(
            text(
                "SELECT ec.company_id AS company, ec.source_id AS source, "
                "       ec.parsed_source_id AS parsed, ec.chunk_set_id AS cs, "
                "       ec.chunk_id AS chunk, "
                "       s.company_id AS src_company, s.artifact_id AS src_artifact, "
                "       ps.source_id AS ps_source, cs.parsed_source_id AS cs_parsed, "
                "       dc.chunk_set_id AS dc_set "
                "FROM evidence_cards ec "
                "JOIN document_chunks dc ON dc.chunk_id = ec.chunk_id "
                "JOIN chunk_sets cs ON cs.chunk_set_id = ec.chunk_set_id "
                "JOIN parsed_sources ps ON ps.parsed_source_id = ec.parsed_source_id "
                "JOIN source_records s ON s.source_id = ec.source_id "
                "JOIN raw_artifacts ra ON ra.artifact_id = s.artifact_id "
                "WHERE ec.evidence_card_id = :cid"
            ).bindparams(cid=extracted.evidence_card_ids[0])
        )
        row = trace.mappings().first()
        assert row is not None
        assert row["company"] == env["company_id"]
        assert row["src_company"] == env["company_id"]
        assert row["source"] == src
        assert row["ps_source"] == src
        assert row["parsed"] == parsed_id
        assert row["cs_parsed"] == parsed_id
        assert row["cs"] == cs_id
        assert row["dc_set"] == cs_id
        assert row["chunk"] == chunk.chunk_id
        assert row["src_artifact"] is not None  # RawArtifact 存在（可读到原始内容）
        # locator 可回到 ParsedSourceBlock。
        block = (
            await session.execute(
                text(
                    "SELECT locator FROM parsed_source_blocks "
                    "WHERE parsed_source_id = :pid AND ordinal = :ord"
                ).bindparams(pid=parsed_id, ord=card.locator_refs[0]["block_ordinal"])
            )
        ).scalar_one()
    assert block == card.locator_refs[0]["locator"]


# ---------------------------------------------------------------- PDF 全链


async def test_pdf_full_pipeline_source_to_evidence_card(env, chroma_manager) -> None:
    """PDF：真实 SourceRecord → 真实解析（pdf_layout v2）→ chunk → index →
    retrieve → extract → EvidenceCard → page/bbox locator（跨页）→ RawArtifact。

    PDF 用真实 SourceIngestionService 上传 + SourceParsingService 解析，不手工
    seed ParsedSource / DocumentChunk。
    """
    pdf = duplicate_line_across_pages_pdf()  # page1: Header/Dup, page2: Dup/Body two
    ingested = await SourceIngestionService(env["sessionmaker"], env["raw_store"]).ingest_upload(
        company_id=env["company_id"],
        provider_key="sse",
        document_type=SourceDocumentType.ANNUAL_REPORT,
        title="季度报告",
        source_url="https://www.sse.com.cn/2026/0809/0002.pdf",
        published_at=_PUBLISHED_AT,
        reporting_period_end=None,
        external_document_id=None,
        stream=io.BytesIO(pdf),
    )
    assert ingested.replayed is False
    src = ingested.record.source_id

    parsed = await SourceParsingService(env["sessionmaker"], env["raw_store"]).parse_source(src)
    assert parsed.parser_name == "pdf_layout"
    assert parsed.parser_version == 2
    assert parsed.block_count == 4  # Header / Dup / Dup / Body two
    chunk_result = await ChunkingService(env["sessionmaker"]).chunk_parsed_source(
        parsed.parsed_source_id
    )
    async with env["sessionmaker"]() as session:
        chunks = await DocumentChunkRepository(session).list_for_chunk_set(
            chunk_result.chunk_set_id
        )
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.text == "Header\nDup\nDup\nBody two"
    # 跨页唯一 quote：block2(page1) + block3(page2) 的 "Dup\nDup"。
    quote = "Dup\nDup"
    assert quote in chunk.text

    collection_name = f"test_fullchain_pdf_{uuid4().hex[:12]}"
    index, retrieval = _index_retrieval(env, chroma_manager, collection_name)
    client = await chroma_manager.get_client()
    try:
        result = await index.index_chunk_set(chunk_result.chunk_set_id)
        assert result.status == "ready"
        hits = await retrieval.retrieve(
            RetrievalQuery(company_id=env["company_id"], query_text="营业收入", top_k=5)
        )
        assert len(hits) == 1
        hit = hits[0]
        assert hit.chunk_id == chunk.chunk_id
        assert hit.text == chunk.text
        assert hit.provider_key == "sse"
        assert hit.authority_tier == 1  # SSE 官方披露
    finally:
        await client.delete_collection(collection_name)

    fake = FakeEvidenceExtractionModel(
        decision=EvidenceExtractionDecision(
            relevant=True,
            items=[
                EvidenceExtractionItem(
                    evidence_statement=_STATEMENT,
                    evidence_type=EvidenceType.METRIC,
                    quote_text=quote,
                    confidence=EvidenceConfidence.HIGH,
                )
            ],
        )
    )
    service = EvidenceExtractionService(env["sessionmaker"], model=fake)
    extracted = await service.extract_from_hit(_QUESTION, hit)
    assert extracted.created_count == 1
    assert len(fake.calls) == 1

    card = await _card_by_id(env["sessionmaker"], extracted.evidence_card_ids[0])
    assert card is not None
    assert card.quote_text == quote
    assert card.quote_sha256 == _sha(quote)
    assert card.provider_key == "sse"
    assert card.authority_tier_snapshot == 1

    # 跨页 page/bbox locator：block2（page1）+ block3（page2）。
    assert len(card.locator_refs) == 2
    assert card.locator_refs[0]["block_ordinal"] == 2
    assert card.locator_refs[0]["locator"]["type"] == "pdf_page"
    assert card.locator_refs[0]["locator"]["page_number"] == 1
    assert card.locator_refs[1]["block_ordinal"] == 3
    assert card.locator_refs[1]["locator"]["type"] == "pdf_page"
    assert card.locator_refs[1]["locator"]["page_number"] == 2
    assert isinstance(card.locator_refs[0]["locator"]["bbox"], list)
    assert len(card.locator_refs[0]["locator"]["bbox"]) == 4

    # 回溯：EvidenceCard → source → RawArtifact（原始 PDF 字节可读且哈希一致）。
    async with env["sessionmaker"]() as session:
        storage_key = (
            await session.execute(
                text(
                    "SELECT ra.storage_key FROM raw_artifacts ra "
                    "JOIN source_records s ON s.artifact_id = ra.artifact_id "
                    "WHERE s.source_id = :sid"
                ).bindparams(sid=src)
            )
        ).scalar_one()
    with env["raw_store"].open(storage_key) as stored:
        assert hashlib.sha256(stored.read()).hexdigest() == _sha_bytes(pdf)
    # EvidenceCard → chunk → parsed_source 回溯。
    async with env["sessionmaker"]() as session:
        parsed_of_card = (
            await session.execute(
                text(
                    "SELECT ps.parsed_source_id FROM parsed_sources ps "
                    "JOIN chunk_sets cs ON cs.parsed_source_id = ps.parsed_source_id "
                    "JOIN document_chunks dc ON dc.chunk_set_id = cs.chunk_set_id "
                    "JOIN evidence_cards ec ON ec.chunk_id = dc.chunk_id "
                    "WHERE ec.evidence_card_id = :cid"
                ).bindparams(cid=extracted.evidence_card_ids[0])
            )
        ).scalar_one()
    assert parsed_of_card == parsed.parsed_source_id


# ---------------------------------------------------------------- 边界


async def test_full_pipeline_creates_no_stage5_report_tables(env, chroma_manager) -> None:
    """Stage 边界：Stage 3 full-chain 只产出 evidence_cards，不产生 Stage 5
    report 表（report_outlines/report_sections/reports/review_issues）。Stage 4
    claims 表由 Stage 4A 单独引入，不在这里约束（用精确阶段边界名，避免以后过期）。"""
    collection_name = f"test_fullchain_boundary_{uuid4().hex[:12]}"
    index, _ = _index_retrieval(env, chroma_manager, collection_name)
    client = await chroma_manager.get_client()
    try:
        src, _, cs_id, _ = await _seed_html_source(env)
        await index.index_chunk_set(cs_id)
        async with env["sessionmaker"]() as session:
            extra = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema='public' "
                        "AND table_name IN ('report_outlines','report_sections',"
                        "'reports','review_issues')"
                    )
                )
            ).scalar_one()
            assert extra == 0
    finally:
        await client.delete_collection(collection_name)
    assert src is not None


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
