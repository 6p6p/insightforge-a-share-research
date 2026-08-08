"""RetrievalService E2E integration tests (stage 3B.2, spec B.10).

需要真实 PostgreSQL（127.0.0.1:5433）。索引构建使用真实
SourceParsingService + ChunkingService + VectorIndexService；embedding 与
Chroma 都使用 Fake（FakeEmbeddingProvider + FakeChromaManager，零真实网络）。

覆盖：
- query→Chroma→PG hydration 全链路：RetrievalHit 字段、ranking 顺序、locator_refs；
- company isolation（PG eligible + Chroma chunk_set_id 白名单双闸）；
- provider / document_type / source_ids / authority_tier / critical-only /
  published range / reporting period range 过滤（PG eligible 与 Chroma where 一致）；
- 只检索 ready manifest；failed / building manifest（Chroma 仍有 records）被排除；
- 旧 chunker / 旧 parser identity 的 ChunkSet 被排除；
- integrity：chunk 缺失 / Chroma metadata 被篡改 → RetrievalIndexIntegrityError；
- Chroma 不可用 → 稳定错误 RetrievalOperationFailed；
- read path：retrieve 不写 PG（0 manifest）、不写 Chroma（record 数不变）。
"""

from datetime import UTC, date, datetime
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
from app.rag.index.contracts import (
    CHROMA_COLLECTION_SCHEMA_VERSION,
    CHROMA_DISTANCE_METRIC,
    compute_collection_name,
)
from app.rag.index.service import VectorIndexService
from app.rag.retrieval.contracts import RetrievalQuery
from app.rag.retrieval.errors import (
    RetrievalIndexIntegrityError,
    RetrievalIndexNotReady,
    RetrievalOperationFailed,
)
from app.rag.retrieval.service import RetrievalService
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

_XINHUA_URL = "https://www.xinhuanet.com/2026/0808/0001.htm"
_CNSTOCK_URL = "https://www.cnstock.com/2026/0808/0002.htm"
_SSE_URL = "https://www.sse.com.cn/2026/0808/0003.htm"
_SOURCE_TITLE = "新闻标题"
_PUBLISHED_AT = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)

# 5×200 字 HTML → 3 chunks [401, 401, 200]，文本互不相同。
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


async def _cleanup(sessionmaker) -> None:
    async with sessionmaker() as session:
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


async def _add_company(sessionmaker, *, security_code="600520") -> object:
    company_id = uuid4()
    async with sessionmaker() as session:
        await CompanyRepository(session).create(
            CompanyModel(
                company_id=company_id,
                exchange="SSE",
                security_code=security_code,
                identity_key=f"SSE:{security_code}",
                board="sse_main",
                official_name="另一家公司",
                short_name="另一家",
                listing_status="listed",
                identity_source_provider_key="sse",
                identity_source_url="https://www.sse.com.cn",
            )
        )
        await session.commit()
    return company_id


def _collection_name() -> str:
    return compute_collection_name(
        spec=_TEST_SPEC,
        collection_schema_version=CHROMA_COLLECTION_SCHEMA_VERSION,
        distance_metric=CHROMA_DISTANCE_METRIC,
    )


async def _seed_and_index(
    env: dict,
    chroma,
    *,
    company_id=None,
    provider_key="xinhuanet",
    document_type="news_article",
    authority_tier=3,
    critical_claim_eligible=False,
    published_at=_PUBLISHED_AT,
    reporting_period_end=None,
    source_url=_XINHUA_URL,
) -> tuple:
    """Seed source → parse → chunk → index（ready manifest + Chroma records）。"""
    company_id = company_id or env["company_id"]
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
            company_id=company_id,
            provider_key=provider_key,
            artifact_id=artifact.artifact_id,
            document_type=document_type,
            title=_SOURCE_TITLE,
            published_at=published_at,
            reporting_period_end=reporting_period_end,
            source_url=source_url,
            acquisition_method="public_html",
            status="available",
            authority_tier_snapshot=authority_tier,
            critical_claim_eligible_snapshot=critical_claim_eligible,
            provider_capabilities_snapshot=[document_type],
            acquired_at=datetime.now(UTC),
        )
        record = await SourceRecordRepository(session).create(record)
        await session.commit()
        source_id = record.source_id
    parsed_service = SourceParsingService(env["sessionmaker"], env["raw_store"])
    parsed = await parsed_service.parse_source(source_id)
    result = await ChunkingService(env["sessionmaker"]).chunk_parsed_source(parsed.parsed_source_id)
    chunk_set_id = result.chunk_set_id
    service = VectorIndexService(
        sessionmaker=env["sessionmaker"],
        embedding_provider=FakeEmbeddingProvider(_TEST_SPEC),
        chroma=chroma,
        collection_name=_collection_name(),
    )
    indexed = await service.index_chunk_set(chunk_set_id)
    assert indexed.status == "ready"
    assert indexed.indexed_chunk_count == 3
    return source_id, parsed.parsed_source_id, chunk_set_id


def _retrieval_service(
    env: dict, chroma, *, provider=None, collection_name=None
) -> RetrievalService:
    return RetrievalService(
        sessionmaker=env["sessionmaker"],
        embedding_provider=provider or FakeEmbeddingProvider(_TEST_SPEC),
        chroma=chroma,
        collection_name=collection_name or _collection_name(),
    )


# ---------------------------------------------------------------- happy path


async def test_retrieve_hydrates_pg_ranking_and_locators(env) -> None:
    chroma = FakeChromaManager()
    source_id, parsed_source_id, chunk_set_id = await _seed_and_index(env, chroma)
    service = _retrieval_service(env, chroma)

    hits = await service.retrieve(
        RetrievalQuery(company_id=env["company_id"], query_text="净利润增长", top_k=5)
    )

    assert len(hits) == 3
    assert [h.rank for h in hits] == [1, 2, 3]
    distances = [h.distance for h in hits]
    assert distances == sorted(distances)  # 距离升序 = 相似度降序
    for hit in hits:
        assert hit.chunk_set_id == chunk_set_id
        assert hit.parsed_source_id == parsed_source_id
        assert hit.source_id == source_id
        assert hit.company_id == env["company_id"]
        assert hit.provider_key == "xinhuanet"
        assert hit.document_type == "news_article"
        assert hit.source_title == _SOURCE_TITLE
        assert hit.source_url == _XINHUA_URL
        assert hit.published_at == _PUBLISHED_AT
        assert hit.authority_tier == 3
        assert hit.critical_claim_eligible is False
        assert isinstance(hit.chunk_ordinal, int) and hit.chunk_ordinal >= 1
        # locator_refs 从 PG hydrate（html_dom v2 每个 chunk 至少一个定位）。
        assert isinstance(hit.locator_refs, list) and hit.locator_refs
        assert "block_ordinal" in hit.locator_refs[0]
        assert hit.text  # 正文来自 PG，不是 Chroma documents


# ---------------------------------------------------------------- filters


async def test_retrieve_company_isolation(env) -> None:
    company_b = await _add_company(env["sessionmaker"])
    chroma = FakeChromaManager()
    await _seed_and_index(env, chroma)
    await _seed_and_index(env, chroma, company_id=company_b, source_url=_CNSTOCK_URL)

    hits_a = await _retrieval_service(env, chroma).retrieve(
        RetrievalQuery(company_id=env["company_id"], query_text="净利润增长", top_k=10)
    )
    assert hits_a
    assert all(h.company_id == env["company_id"] for h in hits_a)

    hits_b = await _retrieval_service(env, chroma).retrieve(
        RetrievalQuery(company_id=company_b, query_text="净利润增长", top_k=10)
    )
    assert hits_b
    assert all(h.company_id == company_b for h in hits_b)


async def test_retrieve_provider_filter(env) -> None:
    chroma = FakeChromaManager()
    await _seed_and_index(env, chroma, provider_key="xinhuanet")
    await _seed_and_index(env, chroma, provider_key="cnstock", source_url=_CNSTOCK_URL)
    service = _retrieval_service(env, chroma)

    hits = await service.retrieve(
        RetrievalQuery(
            company_id=env["company_id"],
            query_text="净利润增长",
            provider_keys=["cnstock"],
            top_k=10,
        )
    )
    assert hits
    assert all(h.provider_key == "cnstock" for h in hits)


async def test_retrieve_document_type_filter(env) -> None:
    chroma = FakeChromaManager()
    await _seed_and_index(env, chroma, document_type="news_article")
    await _seed_and_index(
        env, chroma, document_type="company_announcement", provider_key="sse", source_url=_SSE_URL
    )
    service = _retrieval_service(env, chroma)

    hits = await service.retrieve(
        RetrievalQuery(
            company_id=env["company_id"],
            query_text="净利润增长",
            document_types=["company_announcement"],
            top_k=10,
        )
    )
    assert hits
    assert all(h.document_type == "company_announcement" for h in hits)


async def test_retrieve_source_ids_filter(env) -> None:
    chroma = FakeChromaManager()
    src_a, _, _ = await _seed_and_index(env, chroma)
    src_b, _, _ = await _seed_and_index(env, chroma, source_url=_CNSTOCK_URL)
    assert src_a != src_b
    service = _retrieval_service(env, chroma)

    hits = await service.retrieve(
        RetrievalQuery(
            company_id=env["company_id"], query_text="净利润增长", source_ids=[src_b], top_k=10
        )
    )
    assert hits
    assert all(h.source_id == src_b for h in hits)


async def test_retrieve_authority_tier_filter(env) -> None:
    chroma = FakeChromaManager()
    await _seed_and_index(env, chroma, authority_tier=3)
    await _seed_and_index(env, chroma, authority_tier=1, provider_key="sse", source_url=_SSE_URL)
    service = _retrieval_service(env, chroma)

    hits = await service.retrieve(
        RetrievalQuery(
            company_id=env["company_id"], query_text="净利润增长", authority_tiers=[1], top_k=10
        )
    )
    assert hits
    assert all(h.authority_tier == 1 for h in hits)


async def test_retrieve_critical_claim_eligible_only(env) -> None:
    chroma = FakeChromaManager()
    await _seed_and_index(env, chroma, critical_claim_eligible=False)
    await _seed_and_index(
        env, chroma, critical_claim_eligible=True, provider_key="sse", source_url=_SSE_URL
    )
    service = _retrieval_service(env, chroma)

    hits = await service.retrieve(
        RetrievalQuery(
            company_id=env["company_id"],
            query_text="净利润增长",
            critical_claim_eligible_only=True,
            top_k=10,
        )
    )
    assert hits
    assert all(h.critical_claim_eligible is True for h in hits)


async def test_retrieve_published_range_filter(env) -> None:
    chroma = FakeChromaManager()
    await _seed_and_index(env, chroma, published_at=datetime(2026, 8, 7, tzinfo=UTC))
    await _seed_and_index(
        env, chroma, published_at=datetime(2026, 8, 20, tzinfo=UTC), source_url=_CNSTOCK_URL
    )
    service = _retrieval_service(env, chroma)

    hits = await service.retrieve(
        RetrievalQuery(
            company_id=env["company_id"],
            query_text="净利润增长",
            published_from=datetime(2026, 8, 10, tzinfo=UTC),
            top_k=10,
        )
    )
    assert hits
    assert all(h.published_at == datetime(2026, 8, 20, tzinfo=UTC) for h in hits)


async def test_retrieve_reporting_period_range_filter(env) -> None:
    chroma = FakeChromaManager()
    await _seed_and_index(env, chroma, reporting_period_end=date(2026, 6, 30))
    await _seed_and_index(
        env, chroma, reporting_period_end=date(2026, 12, 31), source_url=_CNSTOCK_URL
    )
    await _seed_and_index(env, chroma, reporting_period_end=None, source_url=_SSE_URL)
    service = _retrieval_service(env, chroma)

    hits = await service.retrieve(
        RetrievalQuery(
            company_id=env["company_id"],
            query_text="净利润增长",
            reporting_period_from=date(2026, 10, 1),
            top_k=10,
        )
    )
    assert hits
    # 只命中 12/31 的 record；6/30 不满足 from，无 reporting_period_end 的被排除。
    assert all(h.reporting_period_end == date(2026, 12, 31) for h in hits)


# ---------------------------------------------------------------- eligible index selection


async def test_retrieve_ready_manifest_only_excludes_failed_and_building(env) -> None:
    chroma = FakeChromaManager()
    _, _, cs_a = await _seed_and_index(env, chroma)
    _, _, cs_b = await _seed_and_index(env, chroma, source_url=_CNSTOCK_URL)
    _, _, cs_c = await _seed_and_index(env, chroma, source_url=_SSE_URL)
    assert len({cs_a, cs_b, cs_c}) == 3
    # cs_b / cs_c 的 Chroma records 仍在，但 manifest 分别置为 failed / building。
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE chunk_vector_indexes SET status = 'failed' WHERE chunk_set_id = :cid"
            ).bindparams(cid=cs_b)
        )
        await session.execute(
            text(
                "UPDATE chunk_vector_indexes SET status = 'building' WHERE chunk_set_id = :cid"
            ).bindparams(cid=cs_c)
        )
        await session.commit()

    hits = await _retrieval_service(env, chroma).retrieve(
        RetrievalQuery(company_id=env["company_id"], query_text="净利润增长", top_k=10)
    )
    assert hits
    assert all(h.chunk_set_id == cs_a for h in hits)


async def test_retrieve_excludes_old_chunker_and_parser(env) -> None:
    chroma = FakeChromaManager()
    _, _, cs_a = await _seed_and_index(env, chroma)
    _, parsed_b, cs_b = await _seed_and_index(env, chroma, source_url=_CNSTOCK_URL)
    _, parsed_c, cs_c = await _seed_and_index(env, chroma, source_url=_SSE_URL)
    assert len({cs_a, cs_b, cs_c}) == 3
    # eligible 要求 chunker/parser 身份与当前精确一致（DB CHECK >= 1）：
    # chunker 从 1 改成 2（版本变更场景）、parser 从 2 改成 1（旧 parser）。
    # Chroma records 仍在，但 eligible 排除 → 检索不到。
    async with env["sessionmaker"]() as session:
        await session.execute(
            text("UPDATE chunk_sets SET chunker_version = 2 WHERE chunk_set_id = :cid").bindparams(
                cid=cs_b
            )
        )
        await session.execute(
            text(
                "UPDATE parsed_sources SET parser_version = 1 WHERE parsed_source_id = :pid"
            ).bindparams(pid=parsed_c)
        )
        await session.commit()

    hits = await _retrieval_service(env, chroma).retrieve(
        RetrievalQuery(company_id=env["company_id"], query_text="净利润增长", top_k=10)
    )
    assert hits
    assert all(h.chunk_set_id == cs_a for h in hits)


# ---------------------------------------------------------------- eligible 完整匹配（3B.2.1）


async def _not_ready_query(env, chroma) -> None:
    with pytest.raises(RetrievalIndexNotReady):
        await _retrieval_service(env, chroma).retrieve(
            RetrievalQuery(company_id=env["company_id"], query_text="净利润增长", top_k=10)
        )


async def test_retrieve_excludes_wrong_embedding_dimension(env) -> None:
    chroma = FakeChromaManager()
    _, _, chunk_set_id = await _seed_and_index(env, chroma)
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE chunk_vector_indexes SET embedding_dimension = 768 "
                "WHERE chunk_set_id = :cid"
            ).bindparams(cid=chunk_set_id)
        )
        await session.commit()
    await _not_ready_query(env, chroma)


async def test_retrieve_excludes_wrong_normalize_embeddings(env) -> None:
    chroma = FakeChromaManager()
    _, _, chunk_set_id = await _seed_and_index(env, chroma)
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE chunk_vector_indexes SET normalize_embeddings = false "
                "WHERE chunk_set_id = :cid"
            ).bindparams(cid=chunk_set_id)
        )
        await session.commit()
    await _not_ready_query(env, chroma)


async def test_retrieve_excludes_wrong_collection_name(env) -> None:
    chroma = FakeChromaManager()
    _, _, chunk_set_id = await _seed_and_index(env, chroma)
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE chunk_vector_indexes SET collection_name = 'some_other_collection' "
                "WHERE chunk_set_id = :cid"
            ).bindparams(cid=chunk_set_id)
        )
        await session.commit()
    await _not_ready_query(env, chroma)


async def test_retrieve_excludes_ready_but_partially_indexed(env) -> None:
    chroma = FakeChromaManager()
    _, _, chunk_set_id = await _seed_and_index(env, chroma)
    # status 仍 ready，但 indexed < expected：不完整索引不进入 eligible。
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE chunk_vector_indexes SET indexed_chunk_count = 2 "
                "WHERE chunk_set_id = :cid AND expected_chunk_count = 3"
            ).bindparams(cid=chunk_set_id)
        )
        await session.commit()
    await _not_ready_query(env, chroma)


async def test_retrieve_collection_metadata_conflict_is_integrity_error(env) -> None:
    chroma = FakeChromaManager()
    await _seed_and_index(env, chroma)
    # 篡改 collection metadata：模拟 embedding schema 变化但未重建索引。
    client = await chroma.get_client()
    collection = await client.get_collection(_collection_name())
    collection.metadata = dict(collection.metadata, model_revision="rev-wrong")

    with pytest.raises(RetrievalIndexIntegrityError):
        await _retrieval_service(env, chroma).retrieve(
            RetrievalQuery(company_id=env["company_id"], query_text="净利润增长", top_k=10)
        )


# ---------------------------------------------------------------- integrity & errors


async def test_retrieve_missing_chunk_integrity_error(env) -> None:
    chroma = FakeChromaManager()
    _, _, chunk_set_id = await _seed_and_index(env, chroma)
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "DELETE FROM document_chunks WHERE chunk_set_id = :cid "
                "AND chunk_id = (SELECT chunk_id FROM document_chunks "
                "WHERE chunk_set_id = :cid ORDER BY ordinal LIMIT 1)"
            ).bindparams(cid=chunk_set_id)
        )
        await session.commit()

    service = _retrieval_service(env, chroma)
    with pytest.raises(RetrievalIndexIntegrityError) as exc:
        await service.retrieve(
            RetrievalQuery(company_id=env["company_id"], query_text="净利润增长", top_k=10)
        )
    assert exc.value.code == "retrieval_index_integrity_error"


async def test_retrieve_wrong_chroma_metadata_integrity_error(env) -> None:
    chroma = FakeChromaManager()
    _, _, chunk_set_id = await _seed_and_index(env, chroma)
    # 篡改一条 Chroma record 的 text_sha256（保持 id / embedding 不变）。
    collection = await chroma.client.get_or_create_collection(_collection_name())
    got = await collection.get(
        where={"chunk_set_id": str(chunk_set_id)}, include=["metadatas", "embeddings"]
    )
    target_id = got["ids"][0]
    meta = dict(got["metadatas"][0])
    meta["text_sha256"] = "f" * 64
    await collection.upsert(ids=[target_id], embeddings=[got["embeddings"][0]], metadatas=[meta])

    service = _retrieval_service(env, chroma)
    with pytest.raises(RetrievalIndexIntegrityError) as exc:
        await service.retrieve(
            RetrievalQuery(company_id=env["company_id"], query_text="净利润增长", top_k=10)
        )
    assert exc.value.code == "retrieval_index_integrity_error"


class FailingChromaManager:
    async def get_client(self):
        raise ConnectionError("chroma unavailable")


async def test_retrieve_chroma_unavailable_stable_error(env) -> None:
    chroma = FakeChromaManager()
    await _seed_and_index(env, chroma)
    service = RetrievalService(
        sessionmaker=env["sessionmaker"],
        embedding_provider=FakeEmbeddingProvider(_TEST_SPEC),
        chroma=FailingChromaManager(),
        collection_name=_collection_name(),
    )

    with pytest.raises(RetrievalOperationFailed) as exc:
        await service.retrieve(
            RetrievalQuery(company_id=env["company_id"], query_text="净利润增长")
        )
    assert exc.value.code == "retrieval_operation_failed"


# ---------------------------------------------------------------- read path only


async def test_retrieve_read_path_writes_zero_manifests(env) -> None:
    chroma = FakeChromaManager()
    await _seed_and_index(env, chroma)
    service = _retrieval_service(env, chroma)

    async with env["sessionmaker"]() as session:
        manifests_before = (
            await session.execute(select(func.count()).select_from(ChunkVectorIndexModel))
        ).scalar_one()
    collection = await chroma.client.get_or_create_collection(_collection_name())
    records_before = await collection.count()

    await service.retrieve(
        RetrievalQuery(company_id=env["company_id"], query_text="净利润增长", top_k=10)
    )

    async with env["sessionmaker"]() as session:
        manifests_after = (
            await session.execute(select(func.count()).select_from(ChunkVectorIndexModel))
        ).scalar_one()
    records_after = await collection.count()
    assert manifests_before == manifests_after == 1  # 不新增 manifest
    assert records_before == records_after == 3  # 不写 Chroma
