"""block_window chunker v1 (stage 3A).

输入完整 ParsedSource + ordered ParsedBlocks（**不得重新读取 RawArtifact
解析**），按 ParsedBlock.ordinal 严格顺序，用**字符窗口**把文本切成 Chunk：

- target_chars=400 / max_chars=500 / overlap=0（字符，不绑定 BGE tokenizer）；
- 尽量合并完整 block，block 之间以 "\n" 连接，合并后不得超过 max_chars；
- 达到 target_chars 后不再与下一个 block 合并（自然结算该 chunk）；
- 单 block > max_chars：先按确定性句末标点（。！？!?；;）切分，段尽量接近
  max_chars；找不到合适标点则 hard split；
- 不删除重复文本（原文重复必须保留）、不跨 ParsedSource、chunk text 非空；
- 每个 Chunk 保存 locator_refs：block_ordinal + char_start/char_end
  （相对原 ParsedBlock.text，Python 字符索引 [start, end)）+ 原 locator，
  保证 Chunk → ParsedBlock locator → ParsedSource → SourceRecord →
  RawArtifact 可完整回溯。
"""

import hashlib
from uuid import UUID

from app.chunking.contracts import (
    CHUNKER_NAME,
    CHUNKER_VERSION,
    Chunk,
    ChunkBlockRef,
    ChunkSetDocument,
)
from app.chunking.errors import ChunkingContractViolation
from app.parsing.contracts import ParsedBlock

TARGET_CHARS = 400
MAX_CHARS = 500
# overlap=0：字符窗口不重叠（无滑动窗口交叠）。overlap>0 的滑动窗口留未来版本。
OVERLAP_CHARS = 0

# 确定性句末标点集合（中英文）：优先在其后切分，避免 hard split。
_SENTENCE_ENDS = "。！？!?；;"


def chunk_parsed_document(
    parsed_source_id: UUID,
    source_parse_fingerprint: str,
    blocks: tuple[ParsedBlock, ...],
) -> ChunkSetDocument:
    """纯函数：ParsedSource + ordered ParsedBlocks → ChunkSetDocument。"""
    chunks = _chunk_blocks(blocks)
    return ChunkSetDocument(
        parsed_source_id=parsed_source_id,
        source_parse_fingerprint=source_parse_fingerprint,
        chunker_name=CHUNKER_NAME,
        chunker_version=CHUNKER_VERSION,
        chunks=chunks,
    )


def _chunk_blocks(blocks: tuple[ParsedBlock, ...]) -> tuple[Chunk, ...]:
    if not blocks:
        raise ChunkingContractViolation("blocks 必须非空")
    for expected, block in enumerate(blocks, start=1):
        if block.ordinal != expected:
            raise ChunkingContractViolation("blocks 的 ordinal 必须连续 1..n")

    chunks: list[Chunk] = []
    buffer_text = ""
    buffer_refs: list[ChunkBlockRef] = []

    def flush() -> None:
        nonlocal buffer_text, buffer_refs
        if buffer_text:
            chunks.append(_make_chunk(len(chunks) + 1, buffer_text, buffer_refs))
            buffer_text = ""
            buffer_refs = []

    for block in blocks:
        if len(block.text) > MAX_CHARS:
            # oversized block：先结算当前 buffer，再按句末标点 / hard split 切分。
            flush()
            for start, end in _split_block(block.text):
                if buffer_text:
                    # 上一切分段与当前段以 "\n" 合并必然超 max_chars（段和
                    # > block 总长 > max_chars），故各自独立成 chunk。
                    flush()
                buffer_text = block.text[start:end]
                buffer_refs = [ChunkBlockRef(block.ordinal, start, end, block.locator)]
            continue

        if buffer_text and len(buffer_text) >= TARGET_CHARS:
            # 当前 chunk 已足够饱满（>= target_chars）：结算，本 block 开启新 chunk。
            flush()
            buffer_text = block.text
            buffer_refs = [ChunkBlockRef(block.ordinal, 0, len(block.text), block.locator)]
            continue

        candidate = _try_merge(buffer_text, block.text)
        if candidate is not None:
            buffer_text = candidate
            buffer_refs.append(ChunkBlockRef(block.ordinal, 0, len(block.text), block.locator))
        else:
            flush()
            buffer_text = block.text
            buffer_refs = [ChunkBlockRef(block.ordinal, 0, len(block.text), block.locator)]

    flush()
    return tuple(chunks)


def _try_merge(buffer_text: str, block_text: str) -> str | None:
    """尝试把整 block 合并进 buffer（"\n" 连接）；超 max_chars 则拒绝。"""
    if not buffer_text:
        return block_text
    candidate = buffer_text + "\n" + block_text
    if len(candidate) <= MAX_CHARS:
        return candidate
    return None


def _split_block(text: str) -> list[tuple[int, int]]:
    """把超过 max_chars 的 block 切分为多个 (start, end)，段长 <= max_chars。

    优先在句末标点（。！？!?；;）后切分，段尽量接近 max_chars；无可用标点
    则 hard split。返回相对原 block.text 的 Python 字符索引 [start, end)。
    """
    segments: list[tuple[int, int]] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + MAX_CHARS, n)
        if end == n:
            segments.append((start, end))
            break
        cut = end
        while cut > start and text[cut - 1] not in _SENTENCE_ENDS:
            cut -= 1
        if cut > start:
            split_at = cut  # 句末标点属于前一段（切在标点之后）
        else:
            split_at = end  # 无可用标点：hard split
        segments.append((start, split_at))
        start = split_at
    return segments


def _make_chunk(ordinal: int, text: str, refs: list[ChunkBlockRef]) -> Chunk:
    return Chunk(
        ordinal=ordinal,
        text=text,
        text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        char_count=len(text),
        locator_refs=tuple(refs),
    )
