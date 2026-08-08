"""VectorIndexService E2E integration tests (stage 3B.1).

需要真实 PostgreSQL（127.0.0.1:5433）。真实 SourceParsingService + 真实
ChunkingService 构建 ChunkSet；VectorIndexService 使用 FakeEmbeddingProvider +
FakeChromaManager（内存），零真实网络。覆盖：
- happy path：index_chunk_set → manifest ready、indexed=expected、Chroma 每
  chunk 一条 record，metadata 覆盖证据链字段；
- metadata where 过滤（company_id）；
- ready replay：不重新 embedding（embed 调用计数不变）、Chroma 不重复写入；
- embedding 失败 → manifest failed + 稳定错误码 → retry 成功；
- Chroma record 被删 → ready replay 抛 VectorIndexIntegrityError，不自动修复；
- 并发 index 同 ChunkSet → PG manifest=1、Chroma 每 chunk record=1、ready；
- 模型 revision 变化 → 新 collection + 新 manifest，旧 manifest / collection 保留
  （collection identity v2）；
- ChunkSet 不存在 → ChunkSetNotFound；collection 冻结 metadata 不一致 →
  VectorCollectionConflict（manifest failed）；
- ChunkSet 完整性被破坏（chunk 被删）→ ChunkSetIntegrityError。
"""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.chunk_vector_index import ChunkVectorIndexModel
from app.db.models.company import CompanyModel
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_record import SourceRecordModel
from app.db.session import DatabaseManager
from app.rag.embedding.contracts import EmbeddingModelSpec
from app.rag.embedding.errors import EmbeddingInputTooLong
from app.rag.index.contracts import (
    CHROMA_COLLECTION_SCHEMA_VERSION,
    CHROMA_DISTANCE_METRIC,
    collection_configuration,
    compute_collection_name,
)
from app.rag.index.errors import (
    ChunkSetIntegrityError,
    ChunkSetNotFound,
    VectorCollectionConflict,
    VectorIndexIntegrityError,
)
from app.rag.index.service import VectorIndexService
from app.repositories.chunk_vector_index_repository import ChunkVectorIndexRepository
from app.repositories.company_repository import CompanyRepository
from app.repositories.raw_artifact_repository import RawArtifactRepository
from app.repositories.source_record_repository import SourceRecordRepository
from app.services.chunking_service import ChunkingService
from app.services.source_parsing_service import SourceParsingService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from tests.embedding.fakes import FakeEmbeddingProvider
from tests.index.fakes import FakeChromaManager

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_XINHUA_URL = "https://www.xinhuanet.com/2026/0807/0001.htm"
_SOURCE_TITLE = "新闻标题"
_PUBLISHED_AT = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)

# 5×200 字 HTML → 3 chunks [401, 401, 200]，文本互不相同。
_MULTI_HTML = (
    "<html><head><title>多段文档</title></head><body><article>"
    + "".join(f"<p>{'甲乙丙丁戊'[i % 5] * 200}</p>" for i in range(5))
    + "</article></body></html>"
).encode()

# 测试用冻结 spec：revision 必须是 immutable（真实 revision 待 Part 10 回填，
# 自动化测试不下载真实模型，revision 用固定测试值）。
_TEST_SPEC = EmbeddingModelSpec(
    model_id="BAAI/bge-small-zh-v1.5",
    dimension=512,
    normalize_embeddings=True,
    query_instruction="为这个句子生成表示以用于检索相关文章：",
    max_input_tokens=512,
    revision="test-revision-001",
)


class CountingEmbeddingProvider(FakeEmbeddingProvider):
    """记录 embed_documents 调用次数（验证 replay 不重新 embedding）。"""

    def __init__(self, spec=_TEST_SPEC) -> None:
        super().__init__(spec)
        self.embed_documents_calls = 0

    def embed_documents(self, texts):
        self.embed_documents_calls += 1
        return super().embed_documents(texts)


class FailingEmbeddingProvider(FakeEmbeddingProvider):
    """embed_documents 总是抛 EmbeddingInputTooLong（模拟输入超长/失败）。"""

    def __init__(self, spec=_TEST_SPEC) -> None:
        super().__init__(spec)

    def embed_documents(self, texts):
        raise EmbeddingInputTooLong()


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


async def _cleanup(sessionmaker) -> None:
    async with sessionmaker() as session:
        # chunk_vector_indexes 持 FK RESTRICT 到 chunk_sets，必须先删。
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


async def _seed_source(env: dict, *, html: bytes = _MULTI_HTML, company_id=None) -> None:
    stored = env["raw_store"].put_html_bytes(html)
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
            company_id=company_id or env["company_id"],
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
        return record.source_id


async def _chunk_set_id(env: dict) -> object:
    source_id = await _seed_source(env)
    parsed_service = SourceParsingService(env["sessionmaker"], env["raw_store"])
    parsed = await parsed_service.parse_source(source_id)
    result = await ChunkingService(env["sessionmaker"]).chunk_parsed_source(parsed.parsed_source_id)
    return result.chunk_set_id


def _collection_name(spec=_TEST_SPEC) -> str:
    return compute_collection_name(
        spec=spec,
        collection_schema_version=CHROMA_COLLECTION_SCHEMA_VERSION,
        distance_metric=CHROMA_DISTANCE_METRIC,
    )


def _service(env: dict, *, provider=None, chroma=None, collection_name=None):
    return VectorIndexService(
        sessionmaker=env["sessionmaker"],
        embedding_provider=provider or FakeEmbeddingProvider(_TEST_SPEC),
        chroma=chroma or FakeChromaManager(),
        collection_name=collection_name,
    )


async def _manifest(env: dict, chunk_set_id, spec=_TEST_SPEC) -> ChunkVectorIndexModel | None:
    async with env["sessionmaker"]() as session:
        return await ChunkVectorIndexRepository(session).get_by_identity(
            chunk_set_id,
            spec.model_id,
            spec.revision,
            CHROMA_COLLECTION_SCHEMA_VERSION,
        )


async def _chroma_records(chroma, chunk_set_id, spec=_TEST_SPEC) -> tuple[list, list]:
    collection = await chroma.client.get_or_create_collection(_collection_name(spec))
    got = await collection.get(where={"chunk_set_id": str(chunk_set_id)}, include=["metadatas"])
    return got["ids"], got["metadatas"]


# ---------------------------------------------------------------- happy path


async def test_happy_path_indexes_all_chunks(env) -> None:
    chunk_set_id = await _chunk_set_id(env)
    chroma = FakeChromaManager()
    service = _service(env, chroma=chroma)

    result = await service.index_chunk_set(chunk_set_id)

    assert result.replayed is False
    assert result.status == "ready"
    assert result.indexed_chunk_count == 3
    assert result.expected_chunk_count == 3

    manifest = await _manifest(env, chunk_set_id)
    assert manifest is not None
    assert manifest.status == "ready"
    assert manifest.indexed_chunk_count == 3
    assert manifest.expected_chunk_count == 3
    assert manifest.embedding_model_id == _TEST_SPEC.model_id
    assert manifest.embedding_model_revision == _TEST_SPEC.revision
    assert manifest.embedding_dimension == 512
    assert manifest.normalize_embeddings is True
    assert manifest.collection_name == _collection_name()
    assert manifest.collection_name.startswith("insightforge_chunks_v2_")
    assert manifest.collection_schema_version == CHROMA_COLLECTION_SCHEMA_VERSION
    assert len(manifest.index_fingerprint) == 64
    assert all(ch in "0123456789abcdef" for ch in manifest.index_fingerprint)
    assert manifest.last_error_code is None
    assert manifest.ready_at is not None

    ids, metadatas = await _chroma_records(chroma, chunk_set_id)
    assert len(ids) == 3
    assert len(set(ids)) == 3  # 每 chunk 恰好一条 record
    for meta in metadatas:
        assert meta["chunk_set_id"] == str(chunk_set_id)
        assert meta["company_id"] == str(env["company_id"])
        assert meta["provider_key"] == "xinhuanet"
        assert meta["document_type"] == "news_article"
        assert meta["authority_tier"] == 3
        assert meta["critical_claim_eligible"] is False
        assert isinstance(meta["published_at_epoch"], int)
        assert isinstance(meta["chunk_ordinal"], int)
        assert len(meta["text_sha256"]) == 64


async def test_metadata_where_filters_company_id(env) -> None:
    chunk_set_id = await _chunk_set_id(env)
    chroma = FakeChromaManager()
    service = _service(env, chroma=chroma)
    await service.index_chunk_set(chunk_set_id)

    collection = await chroma.client.get_or_create_collection(_collection_name())
    mine = await collection.get(where={"company_id": str(env["company_id"])}, include=["metadatas"])
    assert len(mine["ids"]) == 3
    for meta in mine["metadatas"]:
        assert meta["company_id"] == str(env["company_id"])

    other = await collection.get(where={"company_id": str(uuid4())}, include=["metadatas"])
    assert other["ids"] == []


# ---------------------------------------------------------------- replay


async def test_ready_replay_does_not_reembed(env) -> None:
    chunk_set_id = await _chunk_set_id(env)
    provider = CountingEmbeddingProvider()
    chroma = FakeChromaManager()
    service = _service(env, provider=provider, chroma=chroma)

    first = await service.index_chunk_set(chunk_set_id)
    assert first.replayed is False
    embed_calls_after_first = provider.embed_documents_calls
    assert embed_calls_after_first >= 1

    second = await service.index_chunk_set(chunk_set_id)

    assert second.replayed is True
    assert second.vector_index_id == first.vector_index_id
    assert second.status == "ready"
    assert provider.embed_documents_calls == embed_calls_after_first  # 不重新 embedding

    ids, _ = await _chroma_records(chroma, chunk_set_id)
    assert len(ids) == 3  # Chroma 不重复写入


# ---------------------------------------------------------------- 失败 → retry


async def test_embedding_failure_marks_failed_then_retry_succeeds(env) -> None:
    chunk_set_id = await _chunk_set_id(env)
    chroma = FakeChromaManager()

    failing = _service(env, provider=FailingEmbeddingProvider(), chroma=chroma)
    with pytest.raises(EmbeddingInputTooLong):
        await failing.index_chunk_set(chunk_set_id)

    manifest = await _manifest(env, chunk_set_id)
    assert manifest is not None
    assert manifest.status == "failed"
    assert manifest.last_error_code == "embedding_input_too_long"
    assert manifest.indexed_chunk_count == 0

    # retry：同自然身份 → 重置 building → 成功 ready。
    ok = _service(env, chroma=chroma)
    result = await ok.index_chunk_set(chunk_set_id)
    assert result.replayed is False
    assert result.status == "ready"
    manifest = await _manifest(env, chunk_set_id)
    assert manifest.status == "ready"
    assert manifest.vector_index_id == result.vector_index_id


async def test_missing_chroma_record_raises_integrity_error(env) -> None:
    chunk_set_id = await _chunk_set_id(env)
    chroma = FakeChromaManager()
    service = _service(env, chroma=chroma)
    await service.index_chunk_set(chunk_set_id)

    # 模拟 Chroma 部分丢失一条 record（derived index 允许 partial）。
    ids, _ = await _chroma_records(chroma, chunk_set_id)
    collection = await chroma.client.get_or_create_collection(_collection_name())
    await collection.delete(ids=[ids[0]])

    with pytest.raises(VectorIndexIntegrityError) as exc:
        await service.index_chunk_set(chunk_set_id)
    assert exc.value.code == "index_integrity_error"

    # 不自动修复：manifest 仍 ready（缺失在 retrieval read path 暴露，不重嵌）。
    manifest = await _manifest(env, chunk_set_id)
    assert manifest.status == "ready"
    assert manifest.indexed_chunk_count == 3


async def test_revision_change_creates_new_collection_and_manifest(env) -> None:
    """模型 revision 变化 → 确定性新 collection + 新 manifest，旧 manifest 保留。

    不同 revision 使用不同 collection 名称，不会互相覆盖，也不会触发
    VectorCollectionConflict（collection identity v2）。
    """
    chunk_set_id = await _chunk_set_id(env)
    chroma = FakeChromaManager()
    spec_b = EmbeddingModelSpec(
        model_id=_TEST_SPEC.model_id,
        dimension=_TEST_SPEC.dimension,
        normalize_embeddings=_TEST_SPEC.normalize_embeddings,
        query_instruction=_TEST_SPEC.query_instruction,
        max_input_tokens=_TEST_SPEC.max_input_tokens,
        revision="test-revision-002",
    )
    assert _collection_name(spec_b) != _collection_name()

    result_a = await _service(env, chroma=chroma).index_chunk_set(chunk_set_id)
    assert result_a.status == "ready"

    result_b = await _service(
        env, provider=FakeEmbeddingProvider(spec_b), chroma=chroma
    ).index_chunk_set(chunk_set_id)
    assert result_b.status == "ready"
    assert result_b.vector_index_id != result_a.vector_index_id

    manifest_a = await _manifest(env, chunk_set_id, _TEST_SPEC)
    manifest_b = await _manifest(env, chunk_set_id, spec_b)
    assert manifest_a is not None and manifest_b is not None
    assert manifest_a.collection_name == _collection_name()
    assert manifest_b.collection_name == _collection_name(spec_b)
    assert manifest_a.index_fingerprint != manifest_b.index_fingerprint

    # 两个 collection 各自有完整 records，互不覆盖。
    ids_a, _ = await _chroma_records(chroma, chunk_set_id, _TEST_SPEC)
    ids_b, _ = await _chroma_records(chroma, chunk_set_id, spec_b)
    assert len(ids_a) == 3
    assert len(ids_b) == 3


# ---------------------------------------------------------------- 并发


async def test_concurrent_index_single_manifest_and_records(env) -> None:
    chunk_set_id = await _chunk_set_id(env)
    chroma = FakeChromaManager()
    service = _service(env, chroma=chroma)

    results = await asyncio.gather(
        service.index_chunk_set(chunk_set_id),
        service.index_chunk_set(chunk_set_id),
    )

    assert {r.status for r in results} == {"ready"}
    assert {r.vector_index_id for r in results} == {results[0].vector_index_id}

    async with env["sessionmaker"]() as session:
        manifest_count = (
            await session.execute(
                select(func.count())
                .select_from(ChunkVectorIndexModel)
                .where(ChunkVectorIndexModel.chunk_set_id == chunk_set_id)
            )
        ).scalar_one()
    assert manifest_count == 1  # PG manifest 只有 1 行

    ids, _ = await _chroma_records(chroma, chunk_set_id)
    assert len(ids) == 3
    assert len(set(ids)) == 3  # Chroma 每 chunk 恰好 1 条


# ---------------------------------------------------------------- 错误路径


async def test_chunk_set_not_found(env) -> None:
    service = _service(env)
    with pytest.raises(ChunkSetNotFound) as exc:
        await service.index_chunk_set(uuid4())
    assert exc.value.code == "chunk_set_not_found"


async def test_chunk_set_integrity_violation_when_chunk_deleted(env) -> None:
    chunk_set_id = await _chunk_set_id(env)
    service = _service(env)

    async with env["sessionmaker"]() as session:
        await session.execute(
            text("DELETE FROM document_chunks WHERE chunk_set_id = :cid").bindparams(
                cid=chunk_set_id
            )
        )
        await session.commit()

    with pytest.raises(ChunkSetIntegrityError) as exc:
        await service.index_chunk_set(chunk_set_id)
    assert exc.value.code == "chunk_set_integrity_error"

    # 完整性校验先于 manifest 创建：不留半成品 manifest。
    assert await _manifest(env, chunk_set_id) is None


async def test_collection_config_conflict_marks_failed(env) -> None:
    chunk_set_id = await _chunk_set_id(env)
    chroma = FakeChromaManager()
    # 预建同名 collection，但冻结 metadata 与当前 spec 不一致。
    await chroma.client.get_or_create_collection(
        _collection_name(),
        configuration=collection_configuration(),
        metadata={
            "schema_version": CHROMA_COLLECTION_SCHEMA_VERSION + 1,
            "model_id": _TEST_SPEC.model_id,
            "model_revision": _TEST_SPEC.revision,
            "dimension": 512,
            "normalized": True,
            "distance_metric": CHROMA_DISTANCE_METRIC,
        },
    )
    service = _service(env, chroma=chroma)

    with pytest.raises(VectorCollectionConflict) as exc:
        await service.index_chunk_set(chunk_set_id)
    assert exc.value.code == "vector_collection_conflict"

    manifest = await _manifest(env, chunk_set_id)
    assert manifest is not None
    assert manifest.status == "failed"
    assert manifest.last_error_code == "vector_collection_conflict"
