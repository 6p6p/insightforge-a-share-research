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
    """只允许解析已归档的 text/html 与 application/pdf；其他媒体类型拒绝。"""

    code = "unsupported_parse_media_type"
    message = "only text/html and application/pdf raw artifacts are parseable in this stage"


class PdfParseError(ParsingError):
    """PDF 解析失败：bytes 无法解析为合法 PDF（magic 无效 / malformed 等）。"""

    code = "pdf_parse_error"
    message = "pdf parse error"


class PdfEncryptedError(ParsingError):
    """PDF 已加密 / 密码保护；无密码不可读取（稳定错误，不暴露密钥）。"""

    code = "pdf_encrypted_error"
    message = "pdf is encrypted and cannot be parsed without a password"


class PdfResourceLimitError(ParsingError):
    """PDF 超出资源限制（page_count 不在 1..1000 / 提取字符总量超限）。"""

    code = "pdf_resource_limit_exceeded"
    message = "pdf exceeds parsing resource limits"


class PdfTextUnavailable(ParsingError):
    """整个 PDF 无任何可提取文本（纯扫描件 / 纯图像）。OCR 留未来。"""

    code = "pdf_text_unavailable"
    message = "no extractable text found in pdf"


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
