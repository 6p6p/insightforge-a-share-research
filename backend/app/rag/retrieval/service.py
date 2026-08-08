"""Retrieval service (stage 3B.2): RetrievalQuery → Chroma filtered query → PG hydrate.

流程（**纯 read path，不写 PG / Chroma / 不自动 index_chunk_set**）：
1. **Eligible index selection（PostgreSQL 侧）**：先解析当前可检索 ChunkSet——
   ready manifest + **embedding 配置完整匹配**（model_id / revision / dimension /
   normalize_embeddings / collection_schema_version / collection_name） +
   **indexed == expected** + 当前 chunker + 当前 parser identity + company_id +
   RetrievalQuery filters；为空 → RetrievalIndexNotReady。
2. **Query embedding**：`EmbeddingProvider.embed_query(query_text)`（加 BGE query
   instruction；禁止 silent truncation，超长抛 EmbeddingInputTooLong）。
3. **Chroma filtered query**：query 前先校验 collection 与当前 embedding schema
   一致（name == 查询名；metadata 的 schema_version / model_id / model_revision /
   dimension / normalized / distance_metric 与 `build_collection_metadata(spec)`
   完全一致，不一致 → RetrievalIndexIntegrityError，不 query 不修改）；再
   `query_embeddings=[vector]`；where 至少含 `chunk_set_id $in eligible`，再组合
   filters 成单个 `$and`；n_results=top_k；只取 ids / metadatas / distances
   （**不用 documents 作为正文来源**）。
4. **PG hydrate + integrity**：按 chunk_id 批量从 PostgreSQL hydrate
   （DocumentChunk → ChunkSet → ParsedSource → SourceRecord provenance），保持
   Chroma ranking 顺序；chunk_id 去重、distance finite、Chroma metadata 逐 key
   一致；任何不一致 → RetrievalIndexIntegrityError（不自动修复）。

排序只使用 Chroma cosine distance；**无 similarity threshold / reranker / MMR /
BM25 / LLM judge**。top_k 不足时返回实际命中数。
"""

import math
from uuid import UUID

from sqlalchemy import and_, false, or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.chunking.contracts import CHUNKER_NAME, CHUNKER_VERSION
from app.db.models.chunk_set import ChunkSetModel
from app.db.models.chunk_vector_index import ChunkVectorIndexModel
from app.db.models.document_chunk import DocumentChunkModel
from app.db.models.parsed_source import ParsedSourceModel
from app.db.models.source_record import SourceRecordModel
from app.parsing.contracts import _parser_specs
from app.rag.embedding.contracts import EmbeddingProvider
from app.rag.embedding.errors import EmbeddingModelNotConfigured
from app.rag.index.contracts import (
    CHROMA_COLLECTION_SCHEMA_VERSION,
    CHROMA_DISTANCE_METRIC,
    build_collection_metadata,
    compute_collection_name,
)
from app.rag.retrieval.contracts import (
    RetrievalHit,
    RetrievalQuery,
    build_chroma_where,
)
from app.rag.retrieval.errors import (
    RetrievalIndexIntegrityError,
    RetrievalIndexNotReady,
    RetrievalOperationFailed,
    stable_error_code,
)
from app.vectorstore.client import ChromaManager


class RetrievalService:
    """语义检索 read service（PostgreSQL = Source of Truth）。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        embedding_provider: EmbeddingProvider,
        chroma: ChromaManager,
        collection_name: str | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._provider = embedding_provider
        self._chroma = chroma
        if collection_name is not None:
            # 测试注入：真实 Chroma 检索测试用独立 collection（与 index 同名）。
            self._collection_name = collection_name
        else:
            # 生产默认：与 VectorIndexService 相同的 embedding schema 派生名。
            self._collection_name = compute_collection_name(
                spec=embedding_provider.model_info,
                collection_schema_version=CHROMA_COLLECTION_SCHEMA_VERSION,
                distance_metric=CHROMA_DISTANCE_METRIC,
            )

    async def retrieve(self, query: RetrievalQuery) -> list[RetrievalHit]:
        """执行一次语义检索，返回按 Chroma cosine distance 升序的候选 hits。"""
        spec = self._provider.model_info
        if spec.revision is None:
            raise EmbeddingModelNotConfigured(
                f"model {spec.model_id} has no immutable revision configured; "
                "cannot retrieve (automated tests use FakeEmbeddingProvider)"
            )

        # 1. eligible index selection（PostgreSQL，read only）。
        eligible = await self._eligible_chunk_set_ids(query)
        if not eligible:
            raise RetrievalIndexNotReady(
                "no ready vector index for company under current model config"
            )

        # 2. query embedding（加 instruction；禁止 silent truncation）。
        query_vector = self._provider.embed_query(query.query_text)

        # 3. Chroma filtered query。
        where = build_chroma_where(chunk_set_ids=eligible, query=query)
        client = await self._chroma_client()
        collection = await self._get_collection(client)
        try:
            result = await collection.query(
                query_embeddings=[query_vector],
                n_results=query.top_k,
                where=where,
                include=["metadatas", "distances"],
            )
        except Exception as exc:
            raise RetrievalOperationFailed(stable_error_code(exc)) from exc

        ids = list(result.get("ids", [[]])[0])
        metadatas = list(result.get("metadatas", [[]])[0])
        distances = list(result.get("distances", [[]])[0])
        if not (len(ids) == len(metadatas) == len(distances)):
            raise RetrievalIndexIntegrityError(
                "chroma query returned mismatched ids/metadatas/distances"
            )

        # 4. PG hydrate + integrity。
        return await self._hydrate(
            query=query,
            eligible_chunk_set_ids=eligible,
            chroma_ids=ids,
            metadatas=metadatas,
            distances=distances,
        )

    # ------------------------------------------------------------ 内部

    async def _eligible_chunk_set_ids(self, query: RetrievalQuery) -> list[UUID]:
        """从 PostgreSQL 解析当前可检索 ChunkSet ids（read only）。

        必要条件：ready manifest + **embedding 配置完整匹配**（model_id /
        revision / dimension / normalize_embeddings / collection_schema_version /
        collection_name） + **indexed == expected** + 当前 chunker + 当前 parser
        identity + company_id + RetrievalQuery filters。collection_name 对齐
        本服务查询的 collection（生产默认 == `compute_collection_name(spec)`；
        测试注入自定义名时对齐注入名，保证 manifest 指向检索实际查询的 collection）。
        """
        spec = self._provider.model_info
        parser_pairs = list(_parser_specs().items())
        stmt = (
            select(ChunkSetModel.chunk_set_id)
            .join(
                ChunkVectorIndexModel,
                ChunkVectorIndexModel.chunk_set_id == ChunkSetModel.chunk_set_id,
            )
            .join(
                ParsedSourceModel,
                ParsedSourceModel.parsed_source_id == ChunkSetModel.parsed_source_id,
            )
            .join(
                SourceRecordModel,
                SourceRecordModel.source_id == ParsedSourceModel.source_id,
            )
            .where(
                ChunkVectorIndexModel.status == "ready",
                ChunkVectorIndexModel.embedding_model_id == spec.model_id,
                ChunkVectorIndexModel.embedding_model_revision == spec.revision,
                ChunkVectorIndexModel.embedding_dimension == spec.dimension,
                ChunkVectorIndexModel.normalize_embeddings == spec.normalize_embeddings,
                ChunkVectorIndexModel.collection_schema_version == CHROMA_COLLECTION_SCHEMA_VERSION,
                ChunkVectorIndexModel.collection_name == self._collection_name,
                ChunkVectorIndexModel.expected_chunk_count
                == ChunkVectorIndexModel.indexed_chunk_count,
                ChunkSetModel.chunker_name == CHUNKER_NAME,
                ChunkSetModel.chunker_version == CHUNKER_VERSION,
                SourceRecordModel.company_id == query.company_id,
            )
        )
        if parser_pairs:
            stmt = stmt.where(
                or_(
                    *(
                        and_(
                            ParsedSourceModel.parser_name == name,
                            ParsedSourceModel.parser_version == version,
                        )
                        for name, version in parser_pairs
                    )
                )
            )
        else:
            stmt = stmt.where(false())
        stmt = self._apply_source_filters(stmt, query)
        async with self._sessionmaker() as session:
            result = await session.execute(stmt)
            return list(result.scalars().all())

    def _apply_source_filters(self, stmt, query: RetrievalQuery):
        """把 RetrievalQuery 的 source / provider / document / time / authority
        filters 应用到 PG 侧 eligible 查询（与 Chroma where 语义一致）。"""
        if query.source_ids:
            stmt = stmt.where(SourceRecordModel.source_id.in_(query.source_ids))
        if query.provider_keys:
            stmt = stmt.where(SourceRecordModel.provider_key.in_(query.provider_keys))
        if query.document_types:
            stmt = stmt.where(SourceRecordModel.document_type.in_(query.document_types))
        if query.authority_tiers:
            stmt = stmt.where(SourceRecordModel.authority_tier_snapshot.in_(query.authority_tiers))
        if query.critical_claim_eligible_only:
            stmt = stmt.where(SourceRecordModel.critical_claim_eligible_snapshot.is_(True))
        if query.published_from is not None:
            stmt = stmt.where(SourceRecordModel.published_at >= query.published_from)
        if query.published_to is not None:
            stmt = stmt.where(SourceRecordModel.published_at <= query.published_to)
        if query.reporting_period_from is not None:
            stmt = stmt.where(SourceRecordModel.reporting_period_end >= query.reporting_period_from)
        if query.reporting_period_to is not None:
            stmt = stmt.where(SourceRecordModel.reporting_period_end <= query.reporting_period_to)
        return stmt

    async def _chroma_client(self):
        try:
            return await self._chroma.get_client()
        except Exception as exc:
            raise RetrievalOperationFailed(stable_error_code(exc)) from exc

    async def _get_collection(self, client):
        """read path 取 collection：不存在（NotFound）→ RetrievalIndexNotReady，
        **metadata / name 与当前 embedding schema 不一致 → RetrievalIndexIntegrityError
        （不得继续 query、不得自动修改 collection）**；其他 Chroma 失败 → 稳定错误。
        **不创建 collection**（不做 repair/write）。"""
        try:
            collection = await client.get_collection(self._collection_name)
        except Exception as exc:
            if _is_collection_not_found(exc):
                raise RetrievalIndexNotReady(
                    f"collection {self._collection_name} not found for query"
                ) from exc
            raise RetrievalOperationFailed(stable_error_code(exc)) from exc
        self._verify_collection(collection)
        return collection

    def _verify_collection(self, collection) -> None:
        """Collection 与当前 embedding schema 完整一致校验（任何 Chroma query 前）。

        - 实际 collection name == 本服务查询名（生产默认 ==
          `compute_collection_name(current spec)`）；
        - `collection.metadata` 的冻结键（schema_version / model_id /
          model_revision / dimension / normalized / distance_metric）与
          `build_collection_metadata(current spec)` **完全一致**。
        任一不一致 → RetrievalIndexIntegrityError；**不继续 query、不自动修改
        collection**（read path 不做 repair/write）。"""
        actual_name = getattr(collection, "name", None)
        if actual_name is not None and actual_name != self._collection_name:
            raise RetrievalIndexIntegrityError(
                f"collection {self._collection_name}: name {actual_name!r} "
                "does not match the queried collection"
            )
        expected_meta = build_collection_metadata(self._provider.model_info)
        actual_meta = getattr(collection, "metadata", None) or {}
        mismatched = {
            key: (actual_meta.get(key), expected)
            for key, expected in expected_meta.items()
            if actual_meta.get(key) != expected
        }
        if mismatched:
            detail = "; ".join(
                f"{k}={actual_meta.get(k)!r} != {v!r}" for k, v in mismatched.items()
            )
            raise RetrievalIndexIntegrityError(
                f"collection {self._collection_name} metadata mismatch vs current "
                f"embedding schema: {detail}"
            )

    async def _hydrate(
        self,
        *,
        query: RetrievalQuery,
        eligible_chunk_set_ids: list[UUID],
        chroma_ids: list[str],
        metadatas: list[dict | None],
        distances: list[float],
    ) -> list[RetrievalHit]:
        """按 Chroma ranking 顺序从 PostgreSQL hydrate 并做完整性校验。"""
        if not chroma_ids:
            return []
        if len(set(chroma_ids)) != len(chroma_ids):
            raise RetrievalIndexIntegrityError("chroma returned duplicate chunk_ids")
        eligible_set = set(eligible_chunk_set_ids)
        rows = await self._load_hydrated_rows(chroma_ids)
        by_chunk_id = {str(row[0].chunk_id): row for row in rows}

        hits: list[RetrievalHit] = []
        for rank, (chunk_id, meta, dist) in enumerate(
            zip(chroma_ids, metadatas, distances, strict=True), start=1
        ):
            row = by_chunk_id.get(chunk_id)
            if row is None:
                raise RetrievalIndexIntegrityError(
                    f"chroma returned chunk_id {chunk_id} not found in PostgreSQL"
                )
            if not isinstance(dist, (int, float)) or not math.isfinite(float(dist)):
                raise RetrievalIndexIntegrityError(
                    f"chroma returned non-finite distance for {chunk_id}"
                )
            chunk, chunk_set, parsed, record = row
            self._verify_chunk(
                chunk=chunk,
                chunk_set=chunk_set,
                parsed=parsed,
                record=record,
                meta=meta,
                eligible_set=eligible_set,
            )
            hits.append(
                RetrievalHit(
                    rank=rank,
                    chunk_id=chunk.chunk_id,
                    chunk_set_id=chunk.chunk_set_id,
                    parsed_source_id=chunk_set.parsed_source_id,
                    source_id=record.source_id,
                    company_id=record.company_id,
                    text=chunk.text,
                    distance=float(dist),
                    provider_key=record.provider_key,
                    document_type=record.document_type,
                    source_title=record.title,
                    source_url=record.source_url,
                    published_at=record.published_at,
                    reporting_period_end=record.reporting_period_end,
                    authority_tier=record.authority_tier_snapshot,
                    critical_claim_eligible=record.critical_claim_eligible_snapshot,
                    chunk_ordinal=chunk.ordinal,
                    locator_refs=chunk.locator_refs,
                )
            )
        return hits

    def _verify_chunk(
        self,
        *,
        chunk: DocumentChunkModel,
        chunk_set: ChunkSetModel,
        parsed: ParsedSourceModel,
        record: SourceRecordModel,
        meta: dict | None,
        eligible_set: set[UUID],
    ) -> None:
        """Chroma metadata / PG 一致性校验；任何不一致 → RetrievalIndexIntegrityError。"""
        if chunk.chunk_set_id not in eligible_set:
            raise RetrievalIndexIntegrityError(
                f"chunk {chunk.chunk_id} belongs to chunk_set {chunk.chunk_set_id} "
                "not in eligible ready indexes"
            )
        if meta is None:
            raise RetrievalIndexIntegrityError(f"chroma returned no metadata for {chunk.chunk_id}")
        expected = {
            "chunk_id": str(chunk.chunk_id),
            "chunk_set_id": str(chunk.chunk_set_id),
            "parsed_source_id": str(chunk_set.parsed_source_id),
            "source_id": str(record.source_id),
            "company_id": str(record.company_id),
            "provider_key": record.provider_key,
            "document_type": record.document_type,
            "text_sha256": chunk.text_sha256,
        }
        mismatched = {
            key: (meta.get(key), value) for key, value in expected.items() if meta.get(key) != value
        }
        if mismatched:
            detail = "; ".join(f"{k}={meta.get(k)!r} != {v!r}" for k, v in mismatched.items())
            raise RetrievalIndexIntegrityError(
                f"chroma metadata mismatch for {chunk.chunk_id}: {detail}"
            )

    async def _load_hydrated_rows(self, chroma_ids: list[str]) -> list[tuple]:
        """按 chunk_id 批量 hydrate（DocumentChunk → ChunkSet → ParsedSource →
        SourceRecord）。非 UUID id → integrity error（不属于本项目 record）。"""
        chunk_uuids: list[UUID] = []
        for chunk_id in chroma_ids:
            try:
                chunk_uuids.append(UUID(chunk_id))
            except ValueError as exc:
                raise RetrievalIndexIntegrityError(
                    f"chroma returned non-UUID chunk_id {chunk_id!r}"
                ) from exc
        stmt = (
            select(DocumentChunkModel, ChunkSetModel, ParsedSourceModel, SourceRecordModel)
            .join(
                ChunkSetModel,
                ChunkSetModel.chunk_set_id == DocumentChunkModel.chunk_set_id,
            )
            .join(
                ParsedSourceModel,
                ParsedSourceModel.parsed_source_id == ChunkSetModel.parsed_source_id,
            )
            .join(
                SourceRecordModel,
                SourceRecordModel.source_id == ParsedSourceModel.source_id,
            )
            .where(DocumentChunkModel.chunk_id.in_(chunk_uuids))
        )
        async with self._sessionmaker() as session:
            result = await session.execute(stmt)
            return list(result.all())


def _is_collection_not_found(exc: Exception) -> bool:
    """collection 缺失判定：Fake 抛 LookupError，真实 chromadb 抛 NotFoundError。"""
    if isinstance(exc, LookupError):
        return True
    try:
        import chromadb.errors as chroma_errors
    except ImportError:  # pragma: no cover - chromadb 是必装依赖
        return False
    return isinstance(exc, chroma_errors.NotFoundError)
