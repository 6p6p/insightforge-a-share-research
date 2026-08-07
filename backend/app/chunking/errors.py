"""Stable error taxonomy for deterministic chunking (stage 3A).

错误消息不包含：Chunk 正文、完整 raw content、DB URL、absolute path。
"""


class ChunkingError(Exception):
    """确定性分块稳定错误基类。"""

    code = "chunking_error"
    message = "chunking error"

    def __init__(self, message: str | None = None) -> None:
        # 未传 message 时使用类级默认（稳定默认 message），str() 即返回该值；
        # 传了 message 时保留既有按位置传参的调用语义不变。
        super().__init__(message if message is not None else self.message)


class ChunkingContractViolation(ChunkingError):
    """Chunk / ChunkSetDocument 契约校验失败（内部不变量，防御与测试用）。"""

    code = "chunking_contract_violation"


class ParsedSourceNotFound(ChunkingError):
    """分块输入不存在：parsed_source_id 无对应 ParsedSource 快照。"""

    code = "parsed_source_not_found"
    message = "parsed source not found"


class ChunkSetIntegrityError(ChunkingError):
    """已有 ChunkSet replay 时完整性校验失败（存储被篡改/不一致）。

    检测到损坏时抛出，**不自动修复**：ChunkSet 是证据链的一部分，自动重建
    会掩盖原始快照与 ChunkSet 的不一致。默认 message 稳定且不含敏感信息。
    """

    code = "chunk_set_integrity_error"
    message = "chunk set integrity error"


class ChunkSetPersistenceFailed(ChunkingError):
    """ChunkSet 持久化事务失败（DB 层错误，部分/整体回滚）。"""

    code = "chunk_set_persistence_failed"
