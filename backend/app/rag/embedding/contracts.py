"""Embedding contracts (stage 3B.1).

冻结 BGE 模型配置（BAAI/bge-small-zh-v1.5）+ EmbeddingProvider 协议 +
向量契约校验（dimension / finite / L2 norm≈1）。

- BGE 中文检索 query instruction 只加在 query 上，document 不加；
- 任何 embedding 都必须满足：dimension=模型维度、分量全部有限、
  L2 norm≈1（normalize_embeddings=true），否则 EmbeddingContractError；
- 禁止 silent truncation：tokenized 长度（含 special tokens）超过
  max_input_tokens → EmbeddingInputTooLong，而不是静默截断。

model revision 必须 immutable（不能依赖 moving "main"）。真实 revision
待 real BGE smoke（Part 10）解析回填；未解析时置 None，此时 BGE
provider 拒绝加载（EmbeddingModelNotConfigured），自动化测试使用
FakeEmbeddingProvider。
"""

import math
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.rag.embedding.errors import (
    EmbeddingContractError,
    EmbeddingInputTooLong,
)

# 冻结模型：BAAI/bge-small-zh-v1.5
BGE_MODEL_ID = "BAAI/bge-small-zh-v1.5"
BGE_DIMENSION = 512
BGE_NORMALIZE_EMBEDDINGS = True
# BGE 官方中文检索 query instruction（仅 query 加，document 不加）
BGE_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："
# bge-small-zh-v1.5 默认 max_seq_length（含 special tokens）
BGE_MAX_INPUT_TOKENS = 512
# real BGE smoke（Part 10，sentence-transformers 4.1.0）解析出的 immutable
# commit revision，绝不依赖 moving "main"。未配置时该值应保持 None 以让
# provider 拒绝加载（EmbeddingModelNotConfigured）。
BGE_MODEL_REVISION: str = "7999e1d3359715c523056ef9478215996d62a620"

# L2 norm 校验容差：float32 下归一化向量 norm 在 1.0 附近抖动
_L2_NORM_ABS_TOL = 1e-4

EmbeddingVector = list[float]


@dataclass(frozen=True)
class EmbeddingModelSpec:
    """冻结的 embedding 模型配置（不可变，作为 provider 的 model_info）。

    local_path 非空时从本地目录加载（sentence-transformers 本地加载，
    0 网络）——HF 不可达环境的离线部署用；model_id / revision 仍是
    模型身份（provenance 语义），加载路径与身份分离。
    """

    model_id: str
    dimension: int
    normalize_embeddings: bool
    query_instruction: str | None
    max_input_tokens: int
    revision: str | None
    local_path: str | None = None


BGE_SMALL_ZH_V1_5 = EmbeddingModelSpec(
    model_id=BGE_MODEL_ID,
    dimension=BGE_DIMENSION,
    normalize_embeddings=BGE_NORMALIZE_EMBEDDINGS,
    query_instruction=BGE_QUERY_INSTRUCTION,
    max_input_tokens=BGE_MAX_INPUT_TOKENS,
    revision=BGE_MODEL_REVISION,
)


@runtime_checkable
class EmbeddingProvider(Protocol):
    """embedding provider 协议。

    - embed_documents：document 不加 query_instruction；
    - embed_query：加 query_instruction 前缀（BGE 中文检索指令）；
    - 返回的每个向量必须通过 validate_embedding_vector；
    - 任何超长输入抛 EmbeddingInputTooLong（不静默截断）。
    """

    @property
    def model_info(self) -> EmbeddingModelSpec: ...

    def embed_documents(self, texts: list[str]) -> list[EmbeddingVector]: ...

    def embed_query(self, text: str) -> EmbeddingVector: ...

    def token_count(self, text: str) -> int: ...


def ensure_within_token_limit(token_count: int, *, spec: EmbeddingModelSpec) -> None:
    """禁止 silent truncation：tokenized 长度（含 special tokens）超限即抛。"""
    if token_count > spec.max_input_tokens:
        raise EmbeddingInputTooLong(
            f"input has {token_count} tokens, exceeding max_input_tokens={spec.max_input_tokens}"
        )


def validate_embedding_vector(vector: EmbeddingVector, *, spec: EmbeddingModelSpec) -> None:
    """校验单个 embedding 满足模型契约：dimension、全部 finite、L2 norm≈1。

    normalize_embeddings=true 时归一化向量 L2 norm 应严格≈1；非归一化模型
    该契约由 spec 决定（本阶段只冻结归一化模型）。
    """
    if not isinstance(vector, list) or len(vector) != spec.dimension:
        raise EmbeddingContractError(
            f"embedding dimension must be {spec.dimension}, got {len(vector)}"
        )
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EmbeddingContractError("embedding must contain only numeric values")
        if not math.isfinite(float(value)):
            raise EmbeddingContractError("embedding must contain only finite values")
    if spec.normalize_embeddings:
        norm = math.sqrt(sum(float(value) * float(value) for value in vector))
        if not math.isclose(norm, 1.0, rel_tol=0.0, abs_tol=_L2_NORM_ABS_TOL):
            raise EmbeddingContractError(f"embedding L2 norm must be ≈1, got {norm:.6f}")
