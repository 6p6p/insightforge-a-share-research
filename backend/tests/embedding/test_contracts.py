"""Embedding contract unit tests (stage 3B.1).

覆盖冻结模型配置（BAAI/bge-small-zh-v1.5）、EmbeddingProvider 协议、
向量契约校验（dimension / finite / L2 norm≈1）、token 上限（禁止静默截断）。
不下载真实模型。
"""

import math

import pytest

from app.rag.embedding.contracts import (
    BGE_DIMENSION,
    BGE_MAX_INPUT_TOKENS,
    BGE_MODEL_ID,
    BGE_MODEL_REVISION,
    BGE_NORMALIZE_EMBEDDINGS,
    BGE_QUERY_INSTRUCTION,
    BGE_SMALL_ZH_V1_5,
    EmbeddingModelSpec,
    EmbeddingProvider,
    ensure_within_token_limit,
    validate_embedding_vector,
)
from app.rag.embedding.errors import (
    EmbeddingContractError,
    EmbeddingInputTooLong,
)


def test_frozen_bge_spec() -> None:
    """冻结模型配置必须锁定 BAAI/bge-small-zh-v1.5 + 512 维 + 归一化。"""
    assert BGE_MODEL_ID == "BAAI/bge-small-zh-v1.5"
    assert BGE_DIMENSION == 512
    assert BGE_NORMALIZE_EMBEDDINGS is True
    assert BGE_MAX_INPUT_TOKENS == 512
    assert BGE_SMALL_ZH_V1_5.model_id == BGE_MODEL_ID
    assert BGE_SMALL_ZH_V1_5.dimension == BGE_DIMENSION
    assert BGE_SMALL_ZH_V1_5.normalize_embeddings is True
    assert BGE_SMALL_ZH_V1_5.max_input_tokens == BGE_MAX_INPUT_TOKENS
    # revision 未确认时可为 None；一旦回填必须 immutable，绝不依赖 moving "main"
    assert BGE_MODEL_REVISION != "main"


def test_query_instruction_frozen() -> None:
    """BGE 中文检索 query instruction：仅 query 加，document 不加。"""
    assert BGE_QUERY_INSTRUCTION == "为这个句子生成表示以用于检索相关文章："


def _spec(dimension: int = 4, *, normalize: bool = True) -> EmbeddingModelSpec:
    return EmbeddingModelSpec(
        model_id="m",
        dimension=dimension,
        normalize_embeddings=normalize,
        query_instruction=None,
        max_input_tokens=512,
        revision="rev",
    )


class TestValidateEmbeddingVector:
    def test_ok(self) -> None:
        spec = _spec()
        validate_embedding_vector([0.5, 0.5, 0.5, 0.5], spec=spec)  # L2 = 1.0

    def test_wrong_dimension(self) -> None:
        spec = _spec()
        with pytest.raises(EmbeddingContractError):
            validate_embedding_vector([1.0, 0.0, 0.0], spec=spec)

    def test_non_numeric_component(self) -> None:
        spec = _spec()
        with pytest.raises(EmbeddingContractError):
            validate_embedding_vector([True, 0.5, 0.5, 0.5], spec=spec)

    def test_non_finite(self) -> None:
        spec = _spec()
        with pytest.raises(EmbeddingContractError):
            validate_embedding_vector([math.nan, 0.5, 0.5, 0.5], spec=spec)
        with pytest.raises(EmbeddingContractError):
            validate_embedding_vector([math.inf, 0.5, 0.5, 0.5], spec=spec)

    def test_not_unit_norm(self) -> None:
        spec = _spec()
        with pytest.raises(EmbeddingContractError):
            validate_embedding_vector([10.0, 0.0, 0.0, 0.0], spec=spec)

    def test_non_normalized_spec_skips_norm_check(self) -> None:
        spec = _spec(normalize=False)
        # 非归一化模型不做 L2 norm 检查，只查 dimension + finite
        validate_embedding_vector([10.0, 0.0, 0.0, 0.0], spec=spec)


class TestTokenLimit:
    def test_within_limit_ok(self) -> None:
        spec = _spec()
        ensure_within_token_limit(512, spec=spec)

    def test_over_limit_raises(self) -> None:
        spec = _spec()
        with pytest.raises(EmbeddingInputTooLong):
            ensure_within_token_limit(513, spec=spec)


class TestProviderProtocol:
    def test_protocol_recognises_well_formed_provider(self) -> None:
        class GoodProvider:
            @property
            def model_info(self) -> EmbeddingModelSpec:
                return BGE_SMALL_ZH_V1_5

            def embed_documents(self, texts: list[str]) -> list[list[float]]:
                return [[0.0] * 512 for _ in texts]

            def embed_query(self, text: str) -> list[float]:
                return [0.0] * 512

            def token_count(self, text: str) -> int:
                return len(text)

        assert isinstance(GoodProvider(), EmbeddingProvider)

    def test_protocol_rejects_missing_method(self) -> None:
        class IncompleteProvider:
            def embed_documents(self, texts: list[str]) -> list[list[float]]:
                return []

        assert not isinstance(IncompleteProvider(), EmbeddingProvider)
