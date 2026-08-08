"""Stable error taxonomy for vector indexing (stage 3B.1).

错误消息不包含：Chunk 正文、完整 raw content、DB URL、absolute path。
`code` 是稳定错误码：索引失败时写入 chunk_vector_indexes.last_error_code。
"""


class VectorIndexError(Exception):
    """向量索引稳定错误基类。"""

    code = "vector_index_error"
    message = "vector index error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message if message is not None else self.message)


class ChunkSetNotFound(VectorIndexError):
    """索引输入不存在：chunk_set_id 无对应 ChunkSet。"""

    code = "chunk_set_not_found"
    message = "chunk set not found"


class ChunkSetIntegrityError(VectorIndexError):
    """ChunkSet 完整性校验失败（chunk_count 不一致 / ordinal 不连续 / 上游断裂）。

    输入损坏时抛出，**不自动修复**：ChunkSet 是证据链的一部分。
    """

    code = "chunk_set_integrity_error"
    message = "chunk set integrity error"


class VectorCollectionConflict(VectorIndexError):
    """同名 Chroma collection 的冻结配置与当前 spec 不一致。

    探测到冲突时抛出，不覆盖既有 collection：Chroma 是 derived index，但
    覆盖会掩盖配置漂移，必须先人工确认。
    """

    code = "vector_collection_conflict"
    message = "vector collection configuration conflict"


class VectorIndexIntegrityError(VectorIndexError):
    """索引完成后 Chroma records 与 PostgreSQL 不一致（缺失/错误）。

    ready replay 命中时先验证 expected records；缺失/错误抛此错误，
    **不在 retrieval read path 自动修复**。
    """

    code = "index_integrity_error"
    message = "chunk vector index integrity error"


class VectorIndexPersistenceFailed(VectorIndexError):
    """manifest（chunk_vector_indexes）持久化事务失败（DB 层错误）。"""

    code = "index_persistence_failed"
    message = "chunk vector index persistence failed"


def stable_error_code(exc: Exception) -> str:
    """把任意异常映射为稳定 last_error_code（不泄露细节到 message）。"""
    if isinstance(exc, VectorIndexError):
        return exc.code
    # embedding 契约错误（EmbeddingInputTooLong / EmbeddingContractError 等）
    from app.rag.embedding.errors import EmbeddingError

    if isinstance(exc, EmbeddingError):
        return exc.code
    try:
        import chromadb.errors as chroma_errors

        if isinstance(exc, chroma_errors.ChromaError):
            return "chroma_operation_failed"
    except ImportError:  # pragma: no cover - chromadb 是必装依赖
        pass
    return "index_operation_failed"
