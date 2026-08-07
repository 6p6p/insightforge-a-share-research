"""Deterministic parsing (stage 2E.1).

只把已归档的 text/html SourceRecord 确定性解析为可定位结构化文本
（ParsedSource + ParsedBlock）。**不做 Chunk / Embedding / Chroma /
Evidence / LLM。**

公开入口：
- parse_html_bytes：纯函数，归档 HTML 原始字节 → ParsedDocument；
- SourceParsingService：service，SourceRecord → ParsedSource 快照
  （replay / 并发 create-or-get 见 service 文档）。
"""

from app.parsing.contracts import (
    HTML_PARSER_NAME,
    HTML_PARSER_VERSION,
    ParsedBlock,
    ParsedDocument,
)
from app.parsing.html_parser import parse_html_bytes

__all__ = [
    "HTML_PARSER_NAME",
    "HTML_PARSER_VERSION",
    "ParsedBlock",
    "ParsedDocument",
    "parse_html_bytes",
]
