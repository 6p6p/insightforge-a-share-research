"""Stable error taxonomy for deterministic parsing (stage 2E.1).

错误消息不包含：HTML 正文、完整 raw content、DB URL、absolute path。
"""


class ParsingError(Exception):
    """确定性解析稳定错误基类。"""

    code = "parsing_error"
    message = "parsing error"

    def __init__(self, message: str | None = None) -> None:
        # 未传 message 时使用类级默认（稳定默认 message），str() 即返回该值；
        # 传了 message 时保留既有按位置传参的调用语义不变。
        super().__init__(message if message is not None else self.message)


class HtmlParseError(ParsingError):
    """HTML 解析失败：bytes 无法解析为 DOM（空输入 / 编码无法检测等）。"""

    code = "html_parse_error"
    message = "html parse error"


class UnsupportedParseMediaType(ParsingError):
    """只允许解析已归档的 text/html；其他媒体类型拒绝。"""

    code = "unsupported_parse_media_type"
    message = "only text/html raw artifacts are parseable in this stage"


class ParsedSourceIntegrityError(ParsingError):
    """已有 ParsedSource replay 时完整性校验失败（存储被篡改/不一致）。

    检测到损坏时抛出，**不自动修复**：解析快照是证据链的一部分，自动重建
    会掩盖原始归档与快照的不一致。默认 message 稳定且不含敏感信息。
    """

    code = "parsed_source_integrity_error"
    message = "parsed source integrity error"


class ParsedSourcePersistenceFailed(ParsingError):
    """解析快照持久化事务失败（DB 层错误，部分/整体回滚）。"""

    code = "parsed_source_persistence_failed"
