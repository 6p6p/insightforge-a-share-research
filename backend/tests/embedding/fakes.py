"""FakeEmbeddingProvider：自动化测试用的确定性 embedding provider。

不下载真实模型。向量由文本的 SHA-256 种子 + 固定 random 生成，因此：
- 同一文本 → 完全相同的向量；
- 不同文本 → 不同向量（确定性可区分）；
- 维度 = spec.dimension（BGE 冻结 512）、分量有限、L2 norm≈1，
  满足 validate_embedding_vector 契约。
"""

import hashlib
import math
import random

from app.rag.embedding.contracts import (
    BGE_SMALL_ZH_V1_5,
    EmbeddingModelSpec,
    EmbeddingVector,
    validate_embedding_vector,
)


class FakeEmbeddingProvider:
    """Deterministic fake embedding provider (fixed dimension, unit norm)."""

    def __init__(self, spec: EmbeddingModelSpec = BGE_SMALL_ZH_V1_5) -> None:
        self._spec = spec

    @property
    def model_info(self) -> EmbeddingModelSpec:
        return self._spec

    def _embed(self, text: str) -> EmbeddingVector:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        seed = int.from_bytes(digest[:8], "little")
        rng = random.Random(seed)
        vector = [rng.uniform(-1.0, 1.0) for _ in range(self._spec.dimension)]
        norm = math.sqrt(sum(value * value for value in vector))
        vector = [value / norm for value in vector]
        validate_embedding_vector(vector, spec=self._spec)
        return vector

    def embed_documents(self, texts: list[str]) -> list[EmbeddingVector]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> EmbeddingVector:
        return self._embed(text)

    def token_count(self, text: str) -> int:
        # 伪计数（近似字符数）：Fake 不模拟真实 tokenizer 语义。
        return len(text)
