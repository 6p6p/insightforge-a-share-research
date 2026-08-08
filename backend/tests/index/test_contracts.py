"""Vector index contract unit tests (stage 3B.1).

覆盖：collection 冻结配置（cosine、无 embedding function、metadata 冻结键）、
index fingerprint 确定性 / 覆盖字段 / 不含时间戳与状态、Chunk metadata
（primitive、evidence-chain 字段、published_at epoch、不塞 locator_refs）、
稳定错误码映射。不依赖 DB / Chroma / 真实模型。
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.rag.embedding.contracts import EmbeddingModelSpec
from app.rag.embedding.errors import EmbeddingModelNotConfigured
from app.rag.index.contracts import (
    CHROMA_COLLECTION_NAME,
    CHROMA_COLLECTION_SCHEMA_VERSION,
    CHROMA_DISTANCE_METRIC,
    ChunkProvenance,
    build_chunk_metadata,
    build_collection_metadata,
    collection_configuration,
    compute_index_fingerprint,
)
from app.rag.index.errors import (
    ChunkSetIntegrityError,
    VectorCollectionConflict,
    VectorIndexIntegrityError,
    stable_error_code,
)

_PUBLISHED = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)


def _spec(*, revision: str = "rev-abc123", dimension: int = 512, normalize: bool = True):
    return EmbeddingModelSpec(
        model_id="BAAI/bge-small-zh-v1.5",
        dimension=dimension,
        normalize_embeddings=normalize,
        query_instruction="为这个句子生成表示以用于检索相关文章：",
        max_input_tokens=512,
        revision=revision,
    )


def _provenance(**overrides) -> ChunkProvenance:
    base = dict(
        chunk_set_id=uuid4(),
        parsed_source_id=uuid4(),
        source_id=uuid4(),
        company_id=uuid4(),
        provider_key="xinhuanet",
        document_type="news_article",
        authority_tier=3,
        critical_claim_eligible=False,
        published_at=_PUBLISHED,
    )
    base.update(overrides)
    return ChunkProvenance(**base)


class TestCollectionConfiguration:
    def test_cosine_space_and_no_embedding_function(self) -> None:
        config = collection_configuration()
        assert config["hnsw"]["space"] == CHROMA_DISTANCE_METRIC == "cosine"
        assert config["embedding_function"] is None

    def test_frozen_metadata_keys(self) -> None:
        metadata = build_collection_metadata(_spec(revision="rev-xyz"))
        assert metadata["schema_version"] == CHROMA_COLLECTION_SCHEMA_VERSION
        assert metadata["model_id"] == "BAAI/bge-small-zh-v1.5"
        assert metadata["model_revision"] == "rev-xyz"
        assert metadata["dimension"] == 512
        assert metadata["normalized"] is True
        assert metadata["distance_metric"] == "cosine"
        assert set(metadata) == {
            "schema_version",
            "model_id",
            "model_revision",
            "dimension",
            "normalized",
            "distance_metric",
        }

    def test_revision_none_refuses_to_freeze(self) -> None:
        with pytest.raises(EmbeddingModelNotConfigured):
            build_collection_metadata(_spec(revision=None))


class TestIndexFingerprint:
    def _fp(self, spec=None, **kw) -> str:
        spec = spec or _spec()
        kwargs = dict(
            chunk_set_fingerprint="c" * 64,
            spec=spec,
            collection_name=CHROMA_COLLECTION_NAME,
            collection_schema_version=CHROMA_COLLECTION_SCHEMA_VERSION,
            distance_metric=CHROMA_DISTANCE_METRIC,
        )
        kwargs.update(kw)
        return compute_index_fingerprint(**kwargs)

    def test_deterministic_and_sha256(self) -> None:
        a = self._fp()
        b = self._fp()
        assert a == b
        assert len(a) == 64
        assert all(ch in "0123456789abcdef" for ch in a)

    def test_sensitive_to_model_revision(self) -> None:
        assert self._fp(spec=_spec(revision="rev-1")) != self._fp(spec=_spec(revision="rev-2"))

    def test_sensitive_to_dimension_and_normalize(self) -> None:
        assert self._fp(spec=_spec(dimension=768)) != self._fp()
        assert self._fp(spec=_spec(normalize=False)) != self._fp()

    def test_sensitive_to_schema_version_and_collection(self) -> None:
        assert self._fp(collection_schema_version=2) != self._fp()
        assert self._fp(collection_name="other") != self._fp()

    def test_sensitive_to_chunk_set_fingerprint(self) -> None:
        other = compute_index_fingerprint(
            chunk_set_fingerprint="d" * 64,
            spec=_spec(),
            collection_name=CHROMA_COLLECTION_NAME,
            collection_schema_version=CHROMA_COLLECTION_SCHEMA_VERSION,
            distance_metric=CHROMA_DISTANCE_METRIC,
        )
        assert other != self._fp()

    def test_revision_none_raises(self) -> None:
        with pytest.raises(EmbeddingModelNotConfigured):
            self._fp(spec=_spec(revision=None))


class TestChunkMetadata:
    def _meta(self, provenance=None, **kw) -> dict:
        provenance = provenance or _provenance()
        defaults = dict(
            chunk_id=uuid4(),
            chunk_ordinal=1,
            text_sha256="a" * 64,
            provenance=provenance,
        )
        defaults.update(kw)
        return build_chunk_metadata(**defaults)

    def test_required_primitive_fields(self) -> None:
        provenance = _provenance()
        chunk_id = uuid4()
        meta = self._meta(chunk_id=chunk_id, provenance=provenance)
        assert meta["chunk_id"] == str(chunk_id)
        assert meta["chunk_set_id"] == str(provenance.chunk_set_id)
        assert meta["parsed_source_id"] == str(provenance.parsed_source_id)
        assert meta["source_id"] == str(provenance.source_id)
        assert meta["company_id"] == str(provenance.company_id)
        assert meta["provider_key"] == "xinhuanet"
        assert meta["document_type"] == "news_article"
        assert meta["chunk_ordinal"] == 1
        assert meta["text_sha256"] == "a" * 64
        assert meta["authority_tier"] == 3
        assert meta["critical_claim_eligible"] is False
        assert isinstance(meta["published_at_epoch"], int)
        assert all(isinstance(v, (int, str, bool)) for v in meta.values())

    def test_no_locator_refs_in_metadata(self) -> None:
        assert "locator_refs" not in self._meta()

    def test_published_at_epoch_rounds_to_int(self) -> None:
        meta = self._meta(provenance=_provenance(published_at=_PUBLISHED))
        assert meta["published_at_epoch"] == int(_PUBLISHED.timestamp())

    def test_null_published_at_not_fabricated(self) -> None:
        meta = self._meta(provenance=_provenance(published_at=None))
        assert "published_at_epoch" not in meta


class TestStableErrorCode:
    def test_vector_index_errors_map_to_own_code(self) -> None:
        assert stable_error_code(VectorCollectionConflict()) == "vector_collection_conflict"
        assert stable_error_code(VectorIndexIntegrityError()) == "index_integrity_error"
        assert stable_error_code(ChunkSetIntegrityError()) == "chunk_set_integrity_error"

    def test_unknown_error_falls_back(self) -> None:
        assert stable_error_code(RuntimeError("boom")) == "index_operation_failed"
