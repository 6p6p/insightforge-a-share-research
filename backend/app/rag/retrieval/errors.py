"""Stable error taxonomy for retrieval (stage 3B.2).

错误消息不包含：Chunk 正文、完整 raw content、DB URL、absolute path。
`code` 是稳定错误码。检索是 **read path**：不做 repair / 自动重建 / 自动
index_chunk_set；任何不一致暴露稳定错误码，由显式重建收敛。
"""


class RetrievalError(Exception):
    """语义检索稳定错误基类。"""

    code = "retrieval_error"
    message = "retrieval error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message if message is not None else self.message)


class RetrievalQueryError(RetrievalError):
    """RetrievalQuery 契约校验失败（company_id 缺失 / query_text 空或超长 /
    top_k 越界 / 时间非 aware / from > to 等）。"""

    code = "invalid_retrieval_query"
    message = "invalid retrieval query"


class RetrievalIndexNotReady(RetrievalError):
    """当前没有可检索索引：eligible ChunkSet 为空（无 ready manifest /
    无当前配置）或 Chroma collection 缺失。

    检索 read path 不自动 index_chunk_set / 不自动重建。
    """

    code = "retrieval_index_not_ready"
    message = "no ready vector index for query"


class RetrievalIndexIntegrityError(RetrievalError):
    """Chroma 返回的 record 与 PostgreSQL 不一致（chunk 缺失 / chunk_set 不在
    eligible ids / metadata 的 chunk/source/company ID、text_sha256、
    provider/document type 不一致）。

    **不 skip silently / 不自动修复**：PG 是 Source of Truth，Chroma 是
    derived index，不一致必须暴露给调用方。
    """

    code = "retrieval_index_integrity_error"
    message = "retrieval index integrity error"


class RetrievalOperationFailed(RetrievalError):
    """Chroma / 外部操作失败（传输层、异常中断），返回稳定错误码。"""

    code = "retrieval_operation_failed"
    message = "retrieval operation failed"


def stable_error_code(exc: Exception) -> str:
    """把任意异常映射为稳定错误码（不泄露细节到 message）。"""
    if isinstance(exc, RetrievalError):
        return exc.code
    from app.rag.embedding.errors import EmbeddingError

    if isinstance(exc, EmbeddingError):
        return exc.code
    try:
        import chromadb.errors as chroma_errors

        if isinstance(exc, chroma_errors.ChromaError):
            return "chroma_operation_failed"
    except ImportError:  # pragma: no cover - chromadb 是必装依赖
        pass
    return "retrieval_operation_failed"
