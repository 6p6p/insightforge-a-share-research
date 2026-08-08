"""Retrieval service unit tests (stage 3B.2, no DB / no network).

用 stub 替代 `_eligible_chunk_set_ids` / `_load_hydrated_rows`（PG 方法），
FakeChromaManager 提供内存 Chroma。覆盖：
- query instruction：retrieve 走 `embed_query`（真实 provider 会加 instruction），
  不走 `embed_documents`；
- token too long：`EmbeddingInputTooLong` 向上传播（禁止 silent truncation）；
- no threshold：Chroma 返回多少命中就返回多少，不设 similarity threshold；
- company isolation：where 只含 `chunk_set_id $in eligible` 白名单；
- collection 缺失 / eligible 为空 → RetrievalIndexNotReady；
- Chroma 不可用 → 稳定错误 RetrievalOperationFailed；
- PG hydrate 一致性：chunk 缺失 / metadata 不匹配 / chunk_set 不在 eligible →
  RetrievalIndexIntegrityError。
"""

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.rag.embedding.contracts import EmbeddingModelSpec
from app.rag.embedding.errors import EmbeddingInputTooLong
from app.rag.index.contracts import (
    CHROMA_COLLECTION_SCHEMA_VERSION,
    build_collection_metadata,
)
from app.rag.retrieval.contracts import RetrievalQuery
from app.rag.retrieval.errors import (
    RetrievalIndexIntegrityError,
    RetrievalIndexNotReady,
    RetrievalOperationFailed,
)
from app.rag.retrieval.service import RetrievalService
from tests.embedding.fakes import FakeEmbeddingProvider
from tests.index.fakes import FakeChromaManager

pytestmark = pytest.mark.asyncio

_COLLECTION = "test_retrieval_collection"
_TEST_SPEC = EmbeddingModelSpec(
    model_id="BAAI/bge-small-zh-v1.5",
    dimension=512,
    normalize_embeddings=True,
    query_instruction="为这个句子生成表示以用于检索相关文章：",
    max_input_tokens=512,
    revision="test-revision-001",
)
_QUERY_TEXT = "净利润增长"


def _dummy_sessionmaker():
    class _Never:
        def __call__(self):
            raise AssertionError("sessionmaker must not be used in retrieval unit tests")

    return _Never()


def _query(company_id, **overrides) -> RetrievalQuery:
    base = dict(company_id=company_id, query_text=_QUERY_TEXT)
    base.update(overrides)
    return RetrievalQuery(**base)


def _service(*, provider=None, chroma=None, collection_name=_COLLECTION) -> RetrievalService:
    return RetrievalService(
        sessionmaker=_dummy_sessionmaker(),
        embedding_provider=provider or FakeEmbeddingProvider(_TEST_SPEC),
        chroma=chroma or FakeChromaManager(),
        collection_name=collection_name,
    )


def _fake_row(
    chunk_id,
    *,
    chunk_set_id,
    company_id,
    source_id=None,
    parsed_source_id=None,
    ordinal=1,
    text="正文",
    provider_key="xinhuanet",
    document_type="news_article",
):
    source_id = source_id or uuid4()
    parsed_source_id = parsed_source_id or uuid4()
    text_sha256 = hashlib.sha256(text.encode()).hexdigest()
    chunk = SimpleNamespace(
        chunk_id=chunk_id,
        chunk_set_id=chunk_set_id,
        ordinal=ordinal,
        text=text,
        text_sha256=text_sha256,
        locator_refs=[{"block_ordinal": 1, "char_start": 0, "char_end": 2}],
    )
    chunk_set = SimpleNamespace(chunk_set_id=chunk_set_id, parsed_source_id=parsed_source_id)
    parsed = SimpleNamespace(parsed_source_id=parsed_source_id, source_id=source_id)
    record = SimpleNamespace(
        source_id=source_id,
        company_id=company_id,
        provider_key=provider_key,
        document_type=document_type,
        title="标题",
        source_url="https://example.com",
        published_at=datetime(2026, 8, 7, tzinfo=UTC),
        reporting_period_end=None,
        authority_tier_snapshot=3,
        critical_claim_eligible_snapshot=False,
    )
    return (chunk, chunk_set, parsed, record)


def _meta_for_row(row) -> dict:
    chunk, chunk_set, parsed, record = row
    return {
        "chunk_id": str(chunk.chunk_id),
        "chunk_set_id": str(chunk.chunk_set_id),
        "parsed_source_id": str(chunk_set.parsed_source_id),
        "source_id": str(record.source_id),
        "company_id": str(record.company_id),
        "provider_key": record.provider_key,
        "document_type": record.document_type,
        "text_sha256": chunk.text_sha256,
    }


async def _populate_collection(chroma, *, rows, provider) -> object:
    client = await chroma.get_client()
    collection = await client.get_or_create_collection(
        _COLLECTION, metadata=build_collection_metadata(provider.model_info)
    )
    ids = [str(row[0].chunk_id) for row in rows]
    embeddings = provider.embed_documents([row[0].text for row in rows])
    metadatas = [_meta_for_row(row) for row in rows]
    await collection.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas)
    return collection


def _stub_eligible(service: RetrievalService, chunk_set_ids: list[UUID]) -> None:
    async def fake_eligible(query: RetrievalQuery) -> list[UUID]:
        return chunk_set_ids

    service._eligible_chunk_set_ids = fake_eligible


def _stub_load(service: RetrievalService, rows: list) -> None:
    async def fake_load(chroma_ids: list[str]):
        return rows

    service._load_hydrated_rows = fake_load


class FailingChromaManager:
    async def get_client(self):
        raise ConnectionError("chroma unavailable")


class TooLongEmbeddingProvider(FakeEmbeddingProvider):
    def embed_query(self, text):
        raise EmbeddingInputTooLong("input too long (must not silently truncate)")


async def test_retrieve_uses_embed_query_not_documents() -> None:
    chunk_set_id = uuid4()
    company_id = uuid4()
    rows = [_fake_row(uuid4(), chunk_set_id=chunk_set_id, company_id=company_id)]
    provider = FakeEmbeddingProvider(_TEST_SPEC)
    calls: list[str] = []
    original = provider.embed_query
    provider.embed_query = lambda text: (calls.append(text), original(text))[1]
    chroma = FakeChromaManager()
    await _populate_collection(chroma, rows=rows, provider=provider)
    service = _service(provider=provider, chroma=chroma)
    _stub_eligible(service, [chunk_set_id])
    _stub_load(service, rows)

    hits = await service.retrieve(_query(company_id))

    assert calls == [_QUERY_TEXT]  # query_text 原样交给 embed_query
    assert len(hits) == 1


async def test_token_too_long_propagates_not_truncated() -> None:
    service = _service(provider=TooLongEmbeddingProvider(_TEST_SPEC))
    _stub_eligible(service, [uuid4()])

    with pytest.raises(EmbeddingInputTooLong):
        await service.retrieve(_query(uuid4()))


async def test_no_threshold_returns_all_matching_hits() -> None:
    chunk_set_id = uuid4()
    company_id = uuid4()
    rows = [
        _fake_row(
            uuid4(), chunk_set_id=chunk_set_id, company_id=company_id, text=f"片段{i}", ordinal=i
        )
        for i in range(1, 4)
    ]
    provider = FakeEmbeddingProvider(_TEST_SPEC)
    chroma = FakeChromaManager()
    await _populate_collection(chroma, rows=rows, provider=provider)
    service = _service(provider=provider, chroma=chroma)
    _stub_eligible(service, [chunk_set_id])
    _stub_load(service, rows)

    hits = await service.retrieve(_query(company_id, top_k=10))

    # 不设 similarity threshold / reranker：Chroma 返回多少命中就返回多少。
    assert len(hits) == 3
    assert [hit.rank for hit in hits] == [1, 2, 3]
    assert all(isinstance(hit.distance, float) for hit in hits)


async def test_company_isolation_where_uses_chunk_set_whitelist() -> None:
    chunk_set_id = uuid4()
    company_id = uuid4()
    rows = [_fake_row(uuid4(), chunk_set_id=chunk_set_id, company_id=company_id)]
    provider = FakeEmbeddingProvider(_TEST_SPEC)
    chroma = FakeChromaManager()
    collection = await _populate_collection(chroma, rows=rows, provider=provider)
    captured: list[dict] = []
    original_query = collection.query

    async def spy_query(**kwargs):
        captured.append(kwargs)
        return await original_query(**kwargs)

    collection.query = spy_query
    service = _service(provider=provider, chroma=chroma)
    _stub_eligible(service, [chunk_set_id])
    _stub_load(service, rows)

    await service.retrieve(_query(company_id))

    assert captured[0]["query_embeddings"] == [provider.embed_query(_QUERY_TEXT)]
    where = captured[0]["where"]
    assert where == {"chunk_set_id": {"$in": [str(chunk_set_id)]}}


async def test_collection_missing_is_not_ready() -> None:
    service = _service()
    _stub_eligible(service, [uuid4()])

    with pytest.raises(RetrievalIndexNotReady) as exc:
        await service.retrieve(_query(uuid4()))
    assert exc.value.code == "retrieval_index_not_ready"


async def test_eligible_empty_is_not_ready() -> None:
    service = _service()
    _stub_eligible(service, [])

    with pytest.raises(RetrievalIndexNotReady) as exc:
        await service.retrieve(_query(uuid4()))
    assert exc.value.code == "retrieval_index_not_ready"


async def test_chroma_unavailable_is_stable_error() -> None:
    service = _service(chroma=FailingChromaManager())
    _stub_eligible(service, [uuid4()])

    with pytest.raises(RetrievalOperationFailed) as exc:
        await service.retrieve(_query(uuid4()))
    assert exc.value.code == "retrieval_operation_failed"


async def test_missing_chunk_in_pg_is_integrity_error() -> None:
    chunk_set_id = uuid4()
    company_id = uuid4()
    row = _fake_row(uuid4(), chunk_set_id=chunk_set_id, company_id=company_id)
    provider = FakeEmbeddingProvider(_TEST_SPEC)
    chroma = FakeChromaManager()
    await _populate_collection(chroma, rows=[row], provider=provider)
    service = _service(provider=provider, chroma=chroma)
    _stub_eligible(service, [chunk_set_id])
    _stub_load(service, [])  # PG hydrate 查不到任何 chunk

    with pytest.raises(RetrievalIndexIntegrityError) as exc:
        await service.retrieve(_query(company_id))
    assert exc.value.code == "retrieval_index_integrity_error"


async def test_wrong_chroma_text_hash_is_integrity_error() -> None:
    chunk_set_id = uuid4()
    company_id = uuid4()
    row = _fake_row(uuid4(), chunk_set_id=chunk_set_id, company_id=company_id)
    provider = FakeEmbeddingProvider(_TEST_SPEC)
    chroma = FakeChromaManager()
    client = await chroma.get_client()
    collection = await client.get_or_create_collection(
        _COLLECTION, metadata=build_collection_metadata(provider.model_info)
    )
    meta = _meta_for_row(row)
    meta["text_sha256"] = "f" * 64  # 与 PG 不一致
    embeddings = provider.embed_documents([row[0].text])
    await collection.upsert(ids=[str(row[0].chunk_id)], embeddings=embeddings, metadatas=[meta])
    service = _service(provider=provider, chroma=chroma)
    _stub_eligible(service, [chunk_set_id])
    _stub_load(service, [row])

    with pytest.raises(RetrievalIndexIntegrityError) as exc:
        await service.retrieve(_query(company_id))
    assert exc.value.code == "retrieval_index_integrity_error"


async def test_chunk_set_not_in_eligible_is_integrity_error() -> None:
    chunk_set_id = uuid4()
    other_chunk_set_id = uuid4()
    company_id = uuid4()
    row = _fake_row(uuid4(), chunk_set_id=other_chunk_set_id, company_id=company_id)
    provider = FakeEmbeddingProvider(_TEST_SPEC)
    chroma = FakeChromaManager()
    # Chroma metadata 的 chunk_set_id = chunk_set_id（匹配 where 白名单），
    # 但 PG hydrate 出来的 chunk 属于 other_chunk_set_id —— 两者不一致。
    client = await chroma.get_client()
    collection = await client.get_or_create_collection(
        _COLLECTION, metadata=build_collection_metadata(provider.model_info)
    )
    meta = _meta_for_row(row)
    meta["chunk_set_id"] = str(chunk_set_id)
    embeddings = provider.embed_documents([row[0].text])
    await collection.upsert(ids=[str(row[0].chunk_id)], embeddings=embeddings, metadatas=[meta])
    service = _service(provider=provider, chroma=chroma)
    # eligible 只含 chunk_set_id；Chroma 返回的 record 声称属于 chunk_set_id，
    # 但 PG 侧的 chunk 实际属于 other_chunk_set_id。
    _stub_eligible(service, [chunk_set_id])
    _stub_load(service, [row])

    with pytest.raises(RetrievalIndexIntegrityError) as exc:
        await service.retrieve(_query(company_id))
    assert exc.value.code == "retrieval_index_integrity_error"


# ---------------------------------------------------------------- collection metadata 校验


async def _collection_with_metadata(chroma, provider, *, overrides: dict) -> object:
    client = await chroma.get_client()
    meta = build_collection_metadata(provider.model_info)
    meta.update(overrides)
    return await client.get_or_create_collection(_COLLECTION, metadata=meta)


def _spy_query(collection) -> list[dict]:
    calls: list[dict] = []
    original_query = collection.query

    async def spy_query(**kwargs):
        calls.append(kwargs)
        return await original_query(**kwargs)

    collection.query = spy_query
    return calls


async def test_collection_metadata_correct_permits_query() -> None:
    chunk_set_id = uuid4()
    company_id = uuid4()
    row = _fake_row(uuid4(), chunk_set_id=chunk_set_id, company_id=company_id)
    provider = FakeEmbeddingProvider(_TEST_SPEC)
    chroma = FakeChromaManager()
    collection = await _populate_collection(chroma, rows=[row], provider=provider)
    calls = _spy_query(collection)
    service = _service(provider=provider, chroma=chroma)
    _stub_eligible(service, [chunk_set_id])
    _stub_load(service, [row])

    hits = await service.retrieve(_query(company_id))

    assert len(hits) == 1
    assert calls != []  # metadata 一致 → query 确实执行


@pytest.mark.parametrize(
    "overrides",
    [
        {"model_revision": "rev-wrong"},
        {"dimension": 768},
        {"normalized": False},
        {"distance_metric": "l2"},
        {"schema_version": CHROMA_COLLECTION_SCHEMA_VERSION + 1},
    ],
)
async def test_collection_metadata_mismatch_rejected_before_query(overrides: dict) -> None:
    provider = FakeEmbeddingProvider(_TEST_SPEC)
    chroma = FakeChromaManager()
    collection = await _collection_with_metadata(chroma, provider, overrides=overrides)
    calls = _spy_query(collection)
    service = _service(provider=provider, chroma=chroma)
    _stub_eligible(service, [uuid4()])

    with pytest.raises(RetrievalIndexIntegrityError) as exc:
        await service.retrieve(_query(uuid4()))
    assert exc.value.code == "retrieval_index_integrity_error"
    assert calls == []  # 拒绝发生在 Chroma query 之前


async def test_collection_name_mismatch_rejected_before_query() -> None:
    provider = FakeEmbeddingProvider(_TEST_SPEC)
    chroma = FakeChromaManager()
    collection = await _collection_with_metadata(chroma, provider, overrides={})
    collection.name = "some_other_collection"
    calls = _spy_query(collection)
    service = _service(provider=provider, chroma=chroma)
    _stub_eligible(service, [uuid4()])

    with pytest.raises(RetrievalIndexIntegrityError) as exc:
        await service.retrieve(_query(uuid4()))
    assert exc.value.code == "retrieval_index_integrity_error"
    assert calls == []


# ---------------------------------------------------------------- query result integrity 补充


async def test_duplicate_chunk_ids_is_integrity_error() -> None:
    chunk_set_id = uuid4()
    company_id = uuid4()
    chunk_id = uuid4()
    row = _fake_row(chunk_id, chunk_set_id=chunk_set_id, company_id=company_id)
    provider = FakeEmbeddingProvider(_TEST_SPEC)
    chroma = FakeChromaManager()
    collection = await _populate_collection(chroma, rows=[row], provider=provider)
    original_query = collection.query

    async def dup_query(**kwargs):
        result = await original_query(**kwargs)
        result["ids"] = [[str(chunk_id), str(chunk_id)]]
        result["metadatas"] = [[_meta_for_row(row), _meta_for_row(row)]]
        result["distances"] = [[0.1, 0.2]]
        return result

    collection.query = dup_query
    service = _service(provider=provider, chroma=chroma)
    _stub_eligible(service, [chunk_set_id])

    with pytest.raises(RetrievalIndexIntegrityError) as exc:
        await service.retrieve(_query(company_id))
    assert exc.value.code == "retrieval_index_integrity_error"


async def test_non_finite_distance_is_integrity_error() -> None:
    chunk_set_id = uuid4()
    company_id = uuid4()
    row = _fake_row(uuid4(), chunk_set_id=chunk_set_id, company_id=company_id)
    provider = FakeEmbeddingProvider(_TEST_SPEC)
    chroma = FakeChromaManager()
    collection = await _populate_collection(chroma, rows=[row], provider=provider)
    original_query = collection.query

    async def nan_query(**kwargs):
        result = await original_query(**kwargs)
        result["distances"] = [[float("nan")]]
        return result

    collection.query = nan_query
    service = _service(provider=provider, chroma=chroma)
    _stub_eligible(service, [chunk_set_id])
    _stub_load(service, [row])

    with pytest.raises(RetrievalIndexIntegrityError) as exc:
        await service.retrieve(_query(company_id))
    assert exc.value.code == "retrieval_index_integrity_error"
