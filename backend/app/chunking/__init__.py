"""Deterministic document chunking (stage 3A).

只把 ParsedSource + ParsedBlock 快照确定性切分为 ChunkSet + DocumentChunk
（字符窗口，block_window v1）。**不做 Embedding / Chroma / Retrieval /
Evidence / LLM。**

公开入口：
- chunk_parsed_document：纯函数，ordered ParsedBlocks → ChunkSetDocument；
- ChunkingService：service，parsed_source_id → ChunkSet 快照
  （replay / 并发 create-or-get 见 service 文档）。
"""

from app.chunking.chunker import (
    MAX_CHARS,
    OVERLAP_CHARS,
    TARGET_CHARS,
    chunk_parsed_document,
)
from app.chunking.contracts import (
    CHUNKER_NAME,
    CHUNKER_VERSION,
    Chunk,
    ChunkBlockRef,
    ChunkSetDocument,
)

__all__ = [
    "CHUNKER_NAME",
    "CHUNKER_VERSION",
    "Chunk",
    "ChunkBlockRef",
    "ChunkSetDocument",
    "MAX_CHARS",
    "OVERLAP_CHARS",
    "TARGET_CHARS",
    "chunk_parsed_document",
]
