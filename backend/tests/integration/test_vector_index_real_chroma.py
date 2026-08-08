"""VectorIndexService with a real Chroma backend (stage 3B.1, spec #9).

需要真实 PostgreSQL（127.0.0.1:5433）+ 真实 Chroma（127.0.0.1:8002）。
验证：
- index_chunk_set 端到端：ChunkSet → embedding(Fake，确定性) → 真实 Chroma
  upsert → 验证 → manifest ready；
- Chroma collection 冻结 metadata（schema_version/model_id/revision/
  dimension/normalized/distance_metric）真实往返一致，可重复 get_or_create
  不冲突；
- metadata where 过滤（company_id）在真实 Chroma 上生效；
- 使用**独立测试 collection**（uuid 后缀），测试结束删除，不触碰共享
  collection。

不实现 RetrievalService；不下载真实 BGE 模型。
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.company import CompanyModel
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_record import SourceRecordModel
from app.db.session import DatabaseManager
from app.rag.embedding.contracts import EmbeddingModelSpec
from app.rag.index.contracts import (
    build_collection_metadata,
)
from app.rag.index.service import VectorIndexService
from app.repositories.company_repository import CompanyRepository
from app.repositories.raw_artifact_repository import RawArtifactRepository
from app.repositories.source_record_repository import SourceRecordRepository
from app.services.chunking_service import ChunkingService
from app.services.source_parsing_service import SourceParsingService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from app.vectorstore.client import ChromaManager
from tests.embedding.fakes import FakeEmbeddingProvider

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_XINHUA_URL = "https://www.xinhuanet.com/2026/0807/0002.htm"
_SOURCE_TITLE = "新闻标题"
_PUBLISHED_AT = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)

_MULTI_HTML = (
    "<html><head><title>多段文档</title></head><body><article>"
    + "".join(f"<p>{'甲乙丙丁戊'[i % 5] * 200}</p>" for i in range(5))
    + "</article></body></html>"
).encode()

_TEST_SPEC = EmbeddingModelSpec(
    model_id="BAAI/bge-small-zh-v1.5",
    dimension=512,
    normalize_embeddings=True,
    query_instruction="为这个句子生成表示以用于检索相关文章：",
    max_input_tokens=512,
    revision="test-revision-001",
)


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
    import sqlalchemy as sa

    async with sessionmaker() as session:
        await session.execute(sa.text("DELETE FROM chunk_vector_indexes"))
        await session.execute(sa.text("DELETE FROM document_chunks"))
        await session.execute(sa.text("DELETE FROM chunk_sets"))
        await session.execute(sa.text("DELETE FROM parsed_source_blocks"))
        await session.execute(sa.text("DELETE FROM parsed_sources"))
        await session.execute(sa.text("DELETE FROM news_source_verifications"))
        await session.execute(sa.text("DELETE FROM news_discovery_candidates"))
        await session.execute(sa.text("DELETE FROM news_discovery_runs"))
        await session.execute(sa.text("DELETE FROM source_records"))
        await session.execute(sa.text("DELETE FROM raw_artifacts"))
        await session.execute(sa.text("DELETE FROM company_aliases"))
        await session.execute(sa.text("DELETE FROM companies"))
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


async def _seed_and_chunk(env: dict) -> object:
    stored = env["raw_store"].put_html_bytes(_MULTI_HTML)
    async with env["sessionmaker"]() as session:
        artifact = await RawArtifactRepository(session).create(
            RawArtifactModel(
                content_sha256=stored.content_sha256,
                storage_key=stored.storage_key,
                byte_size=stored.byte_size,
                media_type=stored.media_type,
            )
        )
        if artifact is None:
            artifact = await RawArtifactRepository(session).get_by_sha256(stored.content_sha256)
            assert artifact is not None
        record = SourceRecordModel(
            company_id=env["company_id"],
            provider_key="xinhuanet",
            artifact_id=artifact.artifact_id,
            document_type="news_article",
            title=_SOURCE_TITLE,
            published_at=_PUBLISHED_AT,
            source_url=_XINHUA_URL,
            acquisition_method="public_html",
            status="available",
            authority_tier_snapshot=3,
            critical_claim_eligible_snapshot=False,
            provider_capabilities_snapshot=["news_article"],
            acquired_at=datetime.now(UTC),
        )
        record = await SourceRecordRepository(session).create(record)
        await session.commit()
        source_id = record.source_id
    parsed_service = SourceParsingService(env["sessionmaker"], env["raw_store"])
    parsed = await parsed_service.parse_source(source_id)
    result = await ChunkingService(env["sessionmaker"]).chunk_parsed_source(parsed.parsed_source_id)
    return result.chunk_set_id


async def test_real_chroma_roundtrip_and_where_filter(env, chroma_manager) -> None:
    chunk_set_id = await _seed_and_chunk(env)
    collection_name = f"test_vi_{uuid4().hex[:12]}"
    service = VectorIndexService(
        sessionmaker=env["sessionmaker"],
        embedding_provider=FakeEmbeddingProvider(_TEST_SPEC),
        chroma=chroma_manager,
        collection_name=collection_name,
    )

    client = await chroma_manager.get_client()
    try:
        result = await service.index_chunk_set(chunk_set_id)
        assert result.status == "ready"
        assert result.indexed_chunk_count == 3

        # 冻结 metadata 真实往返一致：再 get_or_create 不冲突（服务已自证），
        # 这里直接核对读回的 collection metadata 冻结键。
        collection = await client.get_collection(collection_name)
        actual = dict(collection.metadata or {})
        expected = build_collection_metadata(_TEST_SPEC)
        for key, value in expected.items():
            assert actual.get(key) == value

        # 每 chunk 恰好一条 record（ids 恒返回，无需 include）。
        got = await collection.get()
        ids = got["ids"]
        assert len(ids) == 3
        assert len(set(ids)) == 3

        # metadata where 过滤 company_id 在真实 Chroma 上生效。
        mine = await collection.get(
            where={"company_id": str(env["company_id"])}, include=["metadatas"]
        )
        assert len(mine["ids"]) == 3
        for meta in mine["metadatas"]:
            assert meta["company_id"] == str(env["company_id"])
        other = await collection.get(where={"company_id": str(uuid4())}, include=["metadatas"])
        assert other["ids"] == []
    finally:
        await client.delete_collection(collection_name)


async def test_real_chroma_replay_verifies_existing_records(env, chroma_manager) -> None:
    chunk_set_id = await _seed_and_chunk(env)
    collection_name = f"test_vi_{uuid4().hex[:12]}"
    service = VectorIndexService(
        sessionmaker=env["sessionmaker"],
        embedding_provider=FakeEmbeddingProvider(_TEST_SPEC),
        chroma=chroma_manager,
        collection_name=collection_name,
    )

    client = await chroma_manager.get_client()
    try:
        first = await service.index_chunk_set(chunk_set_id)
        assert first.replayed is False
        second = await service.index_chunk_set(chunk_set_id)
        assert second.replayed is True
        assert second.vector_index_id == first.vector_index_id
        assert second.status == "ready"
    finally:
        await client.delete_collection(collection_name)
