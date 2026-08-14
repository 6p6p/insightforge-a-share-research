"""Vector index service: ChunkSet → embedding → Chroma + PG manifest (stage 3B.1).

index_chunk_set(chunk_set_id) 把一个 ChunkSet 的 DocumentChunk 逐个 embedding
后写入 Chroma collection（cosine、应用侧计算 embedding、确定性 record id =
str(chunk_id)）。collection 名称由 embedding schema fingerprint 纯函数派生
（`insightforge_chunks_v2_<fp12>`，同 schema 所有公司 / ChunkSet 共享；
模型 revision 变化 → 新 collection + 新 manifest，旧 collection 保留）。
PostgreSQL 登记可重建 manifest（chunk_vector_indexes）。PostgreSQL =
Source of Truth，Chroma = derived index（允许 partial rows / 可整体重建）。

流程与不变量：
1. 短 DB session 读 ChunkSet + ordered chunks + provenance metadata → 关闭；
   校验 ChunkSet integrity（chunk_count 一致、ordinal 连续、上游链条完整）。
2. Embedding 与 Chroma 网络操作期间**不持有 PG transaction**；真实 BGE 推理
   经 `asyncio.to_thread` 移出事件循环（V1.1 P0-2：后台 preparation 不得阻塞
   API）。
3. manifest create-or-get（自然身份）→ status=building；retry failed/building；
   ready replay 先验证 expected records，缺失/错误抛 VectorIndexIntegrityError，
   **不在 retrieval read path 自动修复**（不得重新 embedding）。
4. Chroma get/create 兼容 collection（冻结 metadata 不一致 → VectorCollectionConflict）
   → 分批 upsert（≤ CHROMA_UPSERT_BATCH_SIZE）确定性 id → 验证所有 expected
   chunk id 存在且 text_sha256 / chunk_set_id 与 PG 一致。
5. 成功 → status=ready、indexed=expected、ready_at；失败 → status=failed +
   稳定 last_error_code。
6. 并发：两进程同时 index 同 ChunkSet → PG manifest=1、Chroma 每 chunk record=1、
   status=ready（允许重复 embedding/upsert，但不生成第二个 manifest，无进程锁）。
"""

import asyncio
import uuid
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.chunk_set import ChunkSetModel
from app.db.models.chunk_vector_index import ChunkVectorIndexModel
from app.db.models.document_chunk import DocumentChunkModel
from app.rag.embedding.contracts import EmbeddingProvider
from app.rag.embedding.errors import EmbeddingModelNotConfigured
from app.rag.index.contracts import (
    CHROMA_COLLECTION_SCHEMA_VERSION,
    CHROMA_DISTANCE_METRIC,
    CHROMA_UPSERT_BATCH_SIZE,
    ChunkProvenance,
    build_chunk_metadata,
    build_collection_metadata,
    collection_configuration,
    compute_collection_name,
    compute_index_fingerprint,
)
from app.rag.index.errors import (
    ChunkSetIntegrityError,
    ChunkSetNotFound,
    VectorCollectionConflict,
    VectorIndexIntegrityError,
    VectorIndexPersistenceFailed,
    stable_error_code,
)
from app.repositories.chunk_set_repository import ChunkSetRepository
from app.repositories.chunk_vector_index_repository import ChunkVectorIndexRepository
from app.repositories.document_chunk_repository import DocumentChunkRepository
from app.repositories.parsed_source_repository import ParsedSourceRepository
from app.repositories.source_record_repository import SourceRecordRepository
from app.vectorstore.client import ChromaManager


@dataclass(frozen=True)
class VectorIndexResult:
    """一次 index_chunk_set 的结果摘要（不含任何正文文本 / embedding）。"""

    vector_index_id: UUID
    chunk_set_id: UUID
    status: str
    indexed_chunk_count: int
    expected_chunk_count: int
    replayed: bool


class VectorIndexService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        embedding_provider: EmbeddingProvider,
        chroma: ChromaManager,
        collection_name: str | None = None,
        *,
        runtime_scope: str = "production",
    ) -> None:
        self._sessionmaker = sessionmaker
        self._provider = embedding_provider
        self._chroma = chroma
        self._runtime_scope = runtime_scope
        if collection_name is not None:
            # 测试注入：真实 Chroma 测试用独立 collection（uuid 后缀）做隔离。
            self._collection_name = collection_name
        else:
            # 生产默认：由 embedding schema 纯函数派生（revision 未配置 →
            # EmbeddingModelNotConfigured）。同 schema 所有公司 / ChunkSet
            # 共享同一 collection；模型 revision 变化 → 新 collection。
            self._collection_name = compute_collection_name(
                spec=embedding_provider.model_info,
                collection_schema_version=CHROMA_COLLECTION_SCHEMA_VERSION,
                distance_metric=CHROMA_DISTANCE_METRIC,
            )

    async def index_chunk_set(
        self, chunk_set_id: UUID, *, force_rebuild: bool = False
    ) -> VectorIndexResult:
        """把一个 ChunkSet 嵌入并登记到 `self._collection_name`。

        `force_rebuild=True`（默认 False）：**跳过 ready replay**，把既有 manifest
        重置为 building 并重建进当前 collection。eval 每 attempt 隔离场景使用——同一
        ChunkSet（幂等 parse/chunk 复用）在不同 attempt 各自重建进独立的
        per-attempt collection；重建成功后 manifest.collection_name 指向实际写入的
        collection。

        **隔离不变量**：manifest 自然身份含 `runtime_scope`（构造注入）。production
        默认 `"production"`；eval 每 attempt 传 `eval:<variant>:<execution_id.hex>`。
        因此不同 attempt 即使 index 同一个 ChunkSet，也各自命中/创建**自己的** manifest
        row——`force_rebuild` 只重建**当前 scope 自己的** manifest，不会覆盖其它
        attempt 的 manifest。生产默认路径行为不变。
        """
        spec = self._provider.model_info
        if spec.revision is None:
            raise EmbeddingModelNotConfigured(
                f"model {spec.model_id} has no immutable revision configured; "
                "cannot index (automated tests use FakeEmbeddingProvider)"
            )

        # 1. 短 DB session：读 ChunkSet + ordered chunks + provenance → 关闭。
        chunk_set, provenance, chunks = await self._load_chunk_set(chunk_set_id)
        expected = len(chunks)
        fingerprint = compute_index_fingerprint(
            chunk_set_fingerprint=chunk_set.chunk_set_fingerprint,
            spec=spec,
            collection_name=self._collection_name,
            collection_schema_version=CHROMA_COLLECTION_SCHEMA_VERSION,
            distance_metric=CHROMA_DISTANCE_METRIC,
        )

        # 3a. manifest create-or-get（短 transaction，Chroma 操作前提交）。
        vector_index_id, existing = await self._ensure_manifest(
            chunk_set_id, spec, fingerprint, expected, force_rebuild=force_rebuild
        )

        # 4a. Chroma 网络操作：get/create 兼容 collection（不持有 PG transaction）。
        client = await self._chroma.get_client()

        # ready replay：先验证 expected records，缺失/错误抛 VectorIndexIntegrityError，
        # 不重新 embedding、不改状态（不在 retrieval read path 自动修复）。
        # force_rebuild 时跳过——manifest 已被重置为 building，走 build 路径。
        if not force_rebuild and existing is not None and existing.status == "ready":
            collection = await self._compatible_collection(client)
            await self._verify_records(collection, chunks, provenance)
            return VectorIndexResult(
                vector_index_id=vector_index_id,
                chunk_set_id=chunk_set_id,
                status="ready",
                indexed_chunk_count=expected,
                expected_chunk_count=expected,
                replayed=True,
            )

        # 4b. build/retry：collection + 分批 upsert + 验证；任何失败 → manifest=failed。
        try:
            collection = await self._compatible_collection(client)
            await self._upsert_chunks(collection, chunks, provenance)
            await self._verify_records(collection, chunks, provenance)
        except Exception as exc:
            await self._mark_failed(vector_index_id, stable_error_code(exc))
            raise
        await self._mark_ready(vector_index_id, expected, collection_name=self._collection_name)
        return VectorIndexResult(
            vector_index_id=vector_index_id,
            chunk_set_id=chunk_set_id,
            status="ready",
            indexed_chunk_count=expected,
            expected_chunk_count=expected,
            replayed=False,
        )

    # ------------------------------------------------------------------ 内部

    async def _load_chunk_set(
        self, chunk_set_id: UUID
    ) -> tuple[ChunkSetModel, ChunkProvenance, list[DocumentChunkModel]]:
        """短 session 读 ChunkSet + ordered chunks + provenance；关闭后校验 integrity。"""
        async with self._sessionmaker() as session:
            set_repo = ChunkSetRepository(session)
            chunk_set = await set_repo.get_by_id(chunk_set_id)
            if chunk_set is None:
                raise ChunkSetNotFound()
            parsed_repo = ParsedSourceRepository(session)
            parsed = await parsed_repo.get_by_id(chunk_set.parsed_source_id)
            if parsed is None:
                raise ChunkSetIntegrityError()
            record_repo = SourceRecordRepository(session)
            record = await record_repo.get_by_id(parsed.source_id)
            if record is None:
                raise ChunkSetIntegrityError()
            chunk_repo = DocumentChunkRepository(session)
            chunks = await chunk_repo.list_for_chunk_set(chunk_set_id)
            provenance = ChunkProvenance(
                chunk_set_id=chunk_set_id,
                parsed_source_id=chunk_set.parsed_source_id,
                source_id=parsed.source_id,
                company_id=record.company_id,
                provider_key=record.provider_key,
                document_type=record.document_type,
                authority_tier=record.authority_tier_snapshot,
                critical_claim_eligible=record.critical_claim_eligible_snapshot,
                published_at=record.published_at,
                reporting_period_end=record.reporting_period_end,
            )
        # session 已关闭；以下纯校验，不持有 DB 连接。
        if chunk_set.chunk_count != len(chunks):
            raise ChunkSetIntegrityError()
        for index, chunk in enumerate(chunks, start=1):
            if chunk.ordinal != index:
                raise ChunkSetIntegrityError()
        return chunk_set, provenance, chunks

    async def _ensure_manifest(
        self,
        chunk_set_id: UUID,
        spec,
        fingerprint: str,
        expected: int,
        *,
        force_rebuild: bool = False,
    ) -> tuple[UUID, ChunkVectorIndexModel | None]:
        """manifest create-or-get（自然身份，含 `self._runtime_scope`）。

        返回 (vector_index_id, existing 或 None)。

        - 无 manifest → 创建 building；
        - failed / building → 重置为 building（retry，确定性 id + upsert 幂等）；
        - ready → 原样返回（replay 由调用方决定）；`force_rebuild=True` 时 ready 也
          重置为 building（eval 同 scope retry 重建进自己的 collection）。
        """
        async with self._sessionmaker() as session:
            repo = ChunkVectorIndexRepository(session)
            existing = await repo.get_by_identity(
                chunk_set_id,
                spec.model_id,
                spec.revision,
                CHROMA_COLLECTION_SCHEMA_VERSION,
                self._runtime_scope,
            )
            if existing is not None:
                if not force_rebuild and existing.status == "ready":
                    return existing.vector_index_id, existing
                await repo.reset_to_building(
                    existing.vector_index_id, index_fingerprint=fingerprint
                )
                await session.commit()
                return existing.vector_index_id, existing
            manifest = ChunkVectorIndexModel(
                vector_index_id=uuid.uuid4(),
                chunk_set_id=chunk_set_id,
                embedding_model_id=spec.model_id,
                embedding_model_revision=spec.revision,
                runtime_scope=self._runtime_scope,
                embedding_dimension=spec.dimension,
                normalize_embeddings=spec.normalize_embeddings,
                collection_name=self._collection_name,
                collection_schema_version=CHROMA_COLLECTION_SCHEMA_VERSION,
                expected_chunk_count=expected,
                indexed_chunk_count=0,
                index_fingerprint=fingerprint,
                status="building",
                last_error_code=None,
            )
            try:
                manifest, _created = await repo.create_or_get(manifest)
                await session.commit()
            except SQLAlchemyError as exc:
                await session.rollback()
                raise VectorIndexPersistenceFailed() from exc
            return manifest.vector_index_id, manifest

    async def _compatible_collection(self, client) -> object:
        """get-or-create 固定共享 collection，并校验冻结 metadata 一致。

        Chroma 对同名 collection 传入不同 metadata 会静默返回既有 collection，
        因此这里读回并逐键比对；不一致 → VectorCollectionConflict。
        """
        config = collection_configuration()
        expected = build_collection_metadata(self._provider.model_info)
        collection = await client.get_or_create_collection(
            self._collection_name,
            configuration=config,
            metadata=expected,
        )
        actual = dict(collection.metadata or {})
        mismatched = {
            key: (actual.get(key), value)
            for key, value in expected.items()
            if actual.get(key) != value
        }
        if mismatched:
            raise VectorCollectionConflict(
                f"collection {self._collection_name} config mismatch: "
                + ", ".join(f"{k}={actual.get(k)!r} != {v!r}" for k, v in mismatched.items())
            )
        return collection

    async def _upsert_chunks(self, collection, chunks, provenance: ChunkProvenance) -> None:
        for batch in _batched(chunks, CHROMA_UPSERT_BATCH_SIZE):
            ids = [str(chunk.chunk_id) for chunk in batch]
            # 真实 BGE 推理是同步 CPU 密集操作：移到线程池，避免阻塞事件循环
            # （V1.1 P0-2：Web 上传后台 preparation 期间 API 必须保持响应）。
            embeddings = await asyncio.to_thread(
                self._provider.embed_documents, [chunk.text for chunk in batch]
            )
            metadatas = [
                build_chunk_metadata(
                    chunk_id=chunk.chunk_id,
                    chunk_ordinal=chunk.ordinal,
                    text_sha256=chunk.text_sha256,
                    provenance=provenance,
                )
                for chunk in batch
            ]
            await collection.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas)

    async def _verify_records(self, collection, chunks, provenance: ChunkProvenance) -> None:
        """验证所有 expected chunk id 存在且 text_sha256 / chunk_set_id / chunk_id 一致。

        任何缺失或不一致 → VectorIndexIntegrityError（不自动修复）。
        """
        problems: list[str] = []
        for batch in _batched(chunks, CHROMA_UPSERT_BATCH_SIZE):
            ids = [str(chunk.chunk_id) for chunk in batch]
            got = await collection.get(ids=ids, include=["metadatas"])
            found = {str(rid): meta for rid, meta in zip(got["ids"], got["metadatas"], strict=True)}
            for chunk in batch:
                meta = found.get(str(chunk.chunk_id))
                if meta is None:
                    problems.append(str(chunk.chunk_id))
                    continue
                expected = _expected_record(chunk, provenance)
                if any(meta.get(key) != value for key, value in expected.items()):
                    problems.append(str(chunk.chunk_id))
        if problems:
            raise VectorIndexIntegrityError()

    async def _mark_ready(
        self, vector_index_id: UUID, expected: int, *, collection_name: str | None = None
    ) -> None:
        async with self._sessionmaker() as session:
            try:
                repo = ChunkVectorIndexRepository(session)
                await repo.mark_ready(
                    vector_index_id, indexed=expected, collection_name=collection_name
                )
                await session.commit()
            except SQLAlchemyError as exc:
                await session.rollback()
                raise VectorIndexPersistenceFailed() from exc

    async def _mark_failed(self, vector_index_id: UUID, error_code: str) -> None:
        async with self._sessionmaker() as session:
            try:
                repo = ChunkVectorIndexRepository(session)
                await repo.mark_failed(vector_index_id, error_code=error_code)
                await session.commit()
            except SQLAlchemyError as exc:
                await session.rollback()
                raise VectorIndexPersistenceFailed() from exc


def _expected_record(chunk: DocumentChunkModel, provenance: ChunkProvenance) -> dict[str, str]:
    """验证用的最小 record 契约：record 身份 + 内容哈希 + 归属。"""
    return {
        "chunk_id": str(chunk.chunk_id),
        "chunk_set_id": str(provenance.chunk_set_id),
        "text_sha256": chunk.text_sha256,
    }


def _batched(items, size: int):
    batch: list = []
    for item in items:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch
