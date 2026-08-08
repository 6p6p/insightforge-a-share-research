"""Vector index contracts: frozen Chroma collection schema and fingerprints (stage 3B.1).

角色分工：
- PostgreSQL = Source of Truth：DocumentChunk + ChunkSet + provenance 全量、
  权威，且不可静默重建。
- Chroma = **可重建 derived index**：只存"确定性 record id → embedding →
  primitive metadata"，不含 chunk 正文、不含 locator_refs（locator 仍从
  PostgreSQL hydrate）。允许 partial rows / 整体重建。

本模块冻结（collection identity v2）：
- collection 名称由 **embedding schema fingerprint 纯函数** 决定：
  `insightforge_chunks_v2_<fp[:12]>`，其中 fingerprint 至少覆盖
  collection_schema_version / model_id / model_revision / dimension /
  normalize_embeddings / distance_metric；同 schema 的所有公司 / ChunkSet
  共享同一 collection，**不按 company / ChunkSet 拆**。模型 revision 变化 →
  确定性新名称 → 新 collection + 新 manifest，旧 collection / manifest 保留。
- collection metadata 冻结键：schema_version / model_id / model_revision /
  dimension / normalized / distance_metric（同名 collection 配置不一致 →
  VectorCollectionConflict，见 errors）；
- index_fingerprint = canonical JSON + SHA-256（不含 timestamps / DB ID /
  status）；
- 每个 DocumentChunk → 一条 Chroma record（id = str(chunk_id)），metadata
  仅 primitive values，含证据链定位字段（不含 locator_refs）。
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from uuid import UUID

from app.rag.embedding.contracts import EmbeddingModelSpec
from app.rag.embedding.errors import EmbeddingModelNotConfigured

CHROMA_COLLECTION_NAME_PREFIX = "insightforge_chunks_v2"
# collection metadata / manifest 共用的 schema 版本（改名或换结构时递增）。
# v2：collection 名称从固定共享改为 embedding schema fingerprint 派生。
CHROMA_COLLECTION_SCHEMA_VERSION = 2
CHROMA_DISTANCE_METRIC = "cosine"
# upsert/get 校验的保守批量上限（≤ chroma 客户端 max batch size）。
CHROMA_UPSERT_BATCH_SIZE = 100


def collection_configuration() -> dict:
    """Chroma collection 配置：cosine HNSW space、**不配置** embedding function。

    application 自己计算 embedding 并在 upsert 时显式传入；collection 绝不
    依赖 Chroma 内置 embedding function。
    """
    return {
        "hnsw": {"space": CHROMA_DISTANCE_METRIC},
        "embedding_function": None,
    }


def compute_collection_schema_fingerprint(
    *,
    spec: EmbeddingModelSpec,
    collection_schema_version: int,
    distance_metric: str,
) -> str:
    """确定性 embedding schema fingerprint（collection 命名用）。

    至少覆盖：collection_schema_version / model_id / model_revision /
    dimension / normalize_embeddings / distance_metric。**不含** company /
    ChunkSet / timestamps / DB ID（同一 schema 的所有公司 / ChunkSet
    共享同一 collection，模型 revision 变化 → 新 fingerprint → 新 collection）。
    """
    if spec.revision is None:
        raise EmbeddingModelNotConfigured()
    payload = {
        "collection_schema_version": collection_schema_version,
        "model_id": spec.model_id,
        "model_revision": spec.revision,
        "dimension": spec.dimension,
        "normalize_embeddings": spec.normalize_embeddings,
        "distance_metric": distance_metric,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def compute_collection_name(
    *,
    spec: EmbeddingModelSpec,
    collection_schema_version: int,
    distance_metric: str,
) -> str:
    """collection 名称纯函数：`insightforge_chunks_v2_<schema_fp[:12]>`。

    - 同 embedding schema → 同名 → 同 collection（所有公司 / ChunkSet 共享）；
    - 模型 revision 变化 / schema 版本递增 / 维度或归一化或距离度量变化 →
      新名称 → 新 collection + 新 manifest，旧 collection / manifest 保留
      （不覆盖、不写死 revision-specific 分支）。
    """
    fingerprint = compute_collection_schema_fingerprint(
        spec=spec,
        collection_schema_version=collection_schema_version,
        distance_metric=distance_metric,
    )
    return f"{CHROMA_COLLECTION_NAME_PREFIX}_{fingerprint[:12]}"


def build_collection_metadata(spec: EmbeddingModelSpec) -> dict[str, int | str | bool]:
    """Collection 冻结 metadata（用于 create/get 后校验同名 collection 一致）。

    revision 必须 immutable；未配置（None）时拒绝建 collection。
    """
    if spec.revision is None:
        raise EmbeddingModelNotConfigured(
            f"model {spec.model_id} has no immutable revision configured; "
            "cannot freeze collection metadata"
        )
    return {
        "schema_version": CHROMA_COLLECTION_SCHEMA_VERSION,
        "model_id": spec.model_id,
        "model_revision": spec.revision,
        "dimension": spec.dimension,
        "normalized": spec.normalize_embeddings,
        "distance_metric": CHROMA_DISTANCE_METRIC,
    }


def compute_index_fingerprint(
    *,
    chunk_set_fingerprint: str,
    spec: EmbeddingModelSpec,
    collection_name: str,
    collection_schema_version: int,
    distance_metric: str,
) -> str:
    """确定性 SHA-256 指纹（sort_keys + 固定 separators + UTF-8）。

    至少覆盖：chunk_set_fingerprint、embedding model id / revision、
    dimension、normalize flag、collection schema version、distance metric。
    **不得包含** timestamps / DB ID / status / chunk 正文。

    同一 ChunkSet + 同模型配置 → 同一指纹 → 重建命中同一 manifest。
    """
    if spec.revision is None:
        raise EmbeddingModelNotConfigured()
    payload = {
        "chunk_set_fingerprint": chunk_set_fingerprint,
        "embedding_model_id": spec.model_id,
        "embedding_model_revision": spec.revision,
        "embedding_dimension": spec.dimension,
        "normalize_embeddings": spec.normalize_embeddings,
        "collection_name": collection_name,
        "collection_schema_version": collection_schema_version,
        "distance_metric": distance_metric,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class ChunkProvenance:
    """一个 ChunkSet 的证据链 provenance（每条 Chunk 的 Chroma metadata 共用）。

    不含正文 / locator_refs；locator 仍从 PostgreSQL hydrate。
    """

    chunk_set_id: UUID
    parsed_source_id: UUID
    source_id: UUID
    company_id: UUID
    provider_key: str
    document_type: str
    authority_tier: int
    critical_claim_eligible: bool
    published_at: datetime | None
    reporting_period_end: date | None


def build_chunk_metadata(
    *,
    chunk_id: UUID,
    chunk_ordinal: int,
    text_sha256: str,
    provenance: ChunkProvenance,
) -> dict[str, int | str | bool]:
    """一条 Chroma record 的 primitive metadata（Chunk + provenance）。

    - 至少含：chunk_id、chunk_set_id、parsed_source_id、source_id、
      company_id、provider_key、document_type、chunk_ordinal、text_sha256、
      authority_tier、critical_claim_eligible；
    - published_at 有值时额外存 epoch（int），NULL 时不伪造；
    - reporting_period_end 有值时额外存 epoch（int，当日 00:00 UTC），
      NULL 时不伪造；
    - **不塞** locator_refs nested JSON（locator 从 PostgreSQL hydrate）。
    """
    metadata: dict[str, int | str | bool] = {
        "chunk_id": str(chunk_id),
        "chunk_set_id": str(provenance.chunk_set_id),
        "parsed_source_id": str(provenance.parsed_source_id),
        "source_id": str(provenance.source_id),
        "company_id": str(provenance.company_id),
        "provider_key": provenance.provider_key,
        "document_type": provenance.document_type,
        "chunk_ordinal": chunk_ordinal,
        "text_sha256": text_sha256,
        "authority_tier": provenance.authority_tier,
        "critical_claim_eligible": provenance.critical_claim_eligible,
    }
    if provenance.published_at is not None:
        metadata["published_at_epoch"] = int(provenance.published_at.timestamp())
    if provenance.reporting_period_end is not None:
        metadata["reporting_period_end_epoch"] = int(
            datetime.combine(provenance.reporting_period_end, time.min, tzinfo=UTC).timestamp()
        )
    return metadata
