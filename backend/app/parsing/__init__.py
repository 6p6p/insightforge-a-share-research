"""Deterministic parsing (stage 2E.1 / 2E.2).

只把已归档的 text/html 或 application/pdf SourceRecord 确定性解析为可定位
结构化文本（ParsedSource + ParsedBlock）。**不做 Chunk / Embedding / Chroma /
Evidence / LLM。**

公开入口：
- parse_html_bytes / parse_pdf_bytes：纯函数，归档原始字节 → ParsedDocument；
- SourceParsingService：service，SourceRecord → ParsedSource 快照
  （按 media_type dispatch；replay / 并发 create-or-get 见 service 文档）。
"""

from app.parsing.contracts import (
    HTML_PARSER_NAME,
    HTML_PARSER_VERSION,
    PDF_PARSER_NAME,
    PDF_PARSER_VERSION,
    ParsedBlock,
    ParsedDocument,
)
from app.parsing.html_parser import parse_html_bytes
from app.parsing.pdf_parser import parse_pdf_bytes

__all__ = [
    "HTML_PARSER_NAME",
    "HTML_PARSER_VERSION",
    "PDF_PARSER_NAME",
    "PDF_PARSER_VERSION",
    "ParsedBlock",
    "ParsedDocument",
    "parse_html_bytes",
    "parse_pdf_bytes",
]
