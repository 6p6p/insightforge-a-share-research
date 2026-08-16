"""BGE (BAAI) embedding provider (stage 3B.1).

基于 sentence-transformers 的 BAAI/bge-small-zh-v1.5 provider：

- **lazy load**：SentenceTransformer 首次调用时才 import 并加载模型，
  不阻塞 app import / startup，也不在启动时联网下载；
- **immutable revision**：spec.revision 为 None 时抛
  EmbeddingModelNotConfigured（绝不回退到 moving "main"）；
- document 不加 query_instruction，query 加（BGE 中文检索指令）；
- encode 前先校验 token 数（含 special tokens），超限抛
  EmbeddingInputTooLong，禁止 silent truncation；
- 返回前逐向量校验 dimension / finite / L2 norm≈1（EmbeddingContractError）。
"""

import threading
from typing import Any

from app.rag.embedding.contracts import (
    BGE_SMALL_ZH_V1_5,
    EmbeddingModelSpec,
    ensure_within_token_limit,
    validate_embedding_vector,
)
from app.rag.embedding.errors import EmbeddingModelNotConfigured


class BGEProvider:
    """BAAI/bge-small-zh-v1.5 embedding provider（惰性加载，线程安全加载）。"""

    def __init__(self, spec: EmbeddingModelSpec = BGE_SMALL_ZH_V1_5) -> None:
        self._spec = spec
        self._model: Any | None = None
        self._tokenizer: Any | None = None
        self._lock = threading.Lock()

    @property
    def model_info(self) -> EmbeddingModelSpec:
        return self._spec

    def _load(self) -> None:
        """首次调用时加载 SentenceTransformer（lazy，不阻塞 startup）。"""
        if self._model is not None:
            return
        with self._lock:
            if self._model is None:
                # 延迟 import：模型不得在 app import/startup 时自动加载。
                from sentence_transformers import SentenceTransformer

                if self._spec.local_path:
                    # 本地离线模型（HF 不可达环境的受控部署）：路径已 pinned，
                    # 不再走网络检查。
                    self._model = SentenceTransformer(self._spec.local_path)
                else:
                    if self._spec.revision is None:
                        raise EmbeddingModelNotConfigured(
                            f"model {self._spec.model_id} has no immutable revision "
                            "configured; real BGE acceptance pending (automated tests "
                            "use FakeEmbeddingProvider)"
                        )
                    self._model = SentenceTransformer(
                        self._spec.model_id, revision=self._spec.revision
                    )
                self._tokenizer = self._model.tokenizer

    def token_count(self, text: str) -> int:
        """tokenized 长度（含 special tokens，如 [CLS]/[SEP]）。"""
        self._load()
        token_ids = self._tokenizer(text)["input_ids"]
        return len(token_ids)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """document embedding：不加 query_instruction。"""
        self._load()
        for text in texts:
            ensure_within_token_limit(self.token_count(text), spec=self._spec)
        vectors = self._model.encode(texts, normalize_embeddings=self._spec.normalize_embeddings)
        # 兼容 numpy.ndarray（真实 sentence-transformers）与纯 list（测试 fake），
        # 并强转 Python float（np.float64 不满足 isinstance(x, float)）。
        results = [[float(v) for v in vector] for vector in vectors]
        for vector in results:
            validate_embedding_vector(vector, spec=self._spec)
        return results

    def embed_query(self, text: str) -> list[float]:
        """query embedding：加 BGE 中文检索 query_instruction 前缀。"""
        self._load()
        prefixed = text
        if self._spec.query_instruction:
            prefixed = self._spec.query_instruction + text
        ensure_within_token_limit(self.token_count(prefixed), spec=self._spec)
        vector = self._model.encode(
            [prefixed], normalize_embeddings=self._spec.normalize_embeddings
        )[0]
        result = [float(v) for v in vector]
        validate_embedding_vector(result, spec=self._spec)
        return result
