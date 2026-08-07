"""Deterministic chunking contracts (stage 3A).

ChunkSetDocument 是"ParsedSource + 特定 chunker 版本下的确定性分块快照"
（不含 DB ID、不含 created_at），由 chunker（block_window v1）输出，
后续由 ChunkingService 计算 chunk_set_fingerprint 并落库为
chunk_sets + document_chunks。

- Chunk 是"字符窗口"文本块：target_chars / max_chars / overlap 由 chunker
  定义；每个 chunk 保存 locator_refs（原 ParsedBlock 文本片段的定位：
  block_ordinal + 相对原 block.text 的 char 索引 [start, end) + 原 locator）。
- Chunk 可含重复文本（不删除原文重复）；不跨 ParsedSource。
- PDF / HTML 使用同一 Chunk 模型，只是 locator_refs 内 locator 的 type 不同。

ChunkSet 是 ParsedSource 的确定性分块快照，不是 Embedding，不是 Evidence。
本阶段（3A）不创建 Chroma collection / 不生成 Embedding / 不检索。
"""

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from app.chunking.errors import ChunkingContractViolation

CHUNKER_NAME = "block_window"
# v1：Stage 3A 冻结字符窗口规则（target=400 / max=500 / overlap=0、
# 句末标点切分 + hard split、locator_refs char 索引 [start, end)）。
# 同 ParsedSource + 同 chunker version → 同 fingerprint → replay 原 ChunkSet；
# version 变化 → 新 fingerprint → 新 ChunkSet，旧版本保留（可追溯）。
CHUNKER_VERSION = 1


def _chunker_specs() -> dict[str, int]:
    """已注册 chunker：chunker_name → 当前 chunker_version。

    动态读取当前常量（而非 import 时冻结），使测试可通过 monkeypatch 版本号
    模拟旧 chunker（version bump 场景：旧版本 ChunkSet 保留，新版本新快照）。
    """
    return {CHUNKER_NAME: CHUNKER_VERSION}


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ChunkBlockRef:
    """一个 Chunk 对某个原 ParsedBlock 文本片段的引用（定位到归档原文）。

    char_start / char_end 是相对原 ParsedBlock.text 的 Python 字符索引，
    半开区间 [start, end)。locator 原样拷贝 ParsedBlock.locator
    （html_dom：DOM 级定位；pdf_page：页面坐标定位）。
    """

    block_ordinal: int
    char_start: int
    char_end: int
    locator: dict

    def __post_init__(self) -> None:
        if isinstance(self.block_ordinal, bool) or not isinstance(self.block_ordinal, int):
            raise ChunkingContractViolation("block_ordinal 必须是 int")
        if self.block_ordinal < 1:
            raise ChunkingContractViolation("block_ordinal 必须 >= 1")
        for name, value in (
            ("char_start", self.char_start),
            ("char_end", self.char_end),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ChunkingContractViolation(f"{name} 必须是 int")
        if self.char_start < 0:
            raise ChunkingContractViolation("char_start 必须 >= 0")
        if self.char_end <= self.char_start:
            raise ChunkingContractViolation("char_end 必须 > char_start")
        if not isinstance(self.locator, dict):
            raise ChunkingContractViolation("locator 必须是 dict")


@dataclass(frozen=True)
class Chunk:
    """一个确定性文本块（字符窗口，非 token）。"""

    ordinal: int
    text: str
    text_sha256: str
    char_count: int
    locator_refs: tuple[ChunkBlockRef, ...]

    def __post_init__(self) -> None:
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int):
            raise ChunkingContractViolation("ordinal 必须是 int")
        if self.ordinal < 1:
            raise ChunkingContractViolation("ordinal 必须 >= 1")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ChunkingContractViolation("text 必须非空")
        if not isinstance(self.text_sha256, str) or len(self.text_sha256) != 64:
            raise ChunkingContractViolation("text_sha256 必须是 64 位 hex")
        if self.text_sha256 != _sha256_hex(self.text):
            raise ChunkingContractViolation("text_sha256 必须等于 text 的 SHA-256")
        if isinstance(self.char_count, bool) or not isinstance(self.char_count, int):
            raise ChunkingContractViolation("char_count 必须是 int")
        if self.char_count != len(self.text):
            raise ChunkingContractViolation("char_count 必须等于 len(text)")
        if not isinstance(self.locator_refs, tuple) or not self.locator_refs:
            raise ChunkingContractViolation("locator_refs 必须是非空 tuple")


@dataclass(frozen=True)
class ChunkSetDocument:
    """一次 chunking 的确定性输出（不含 DB ID / created_at）。"""

    parsed_source_id: UUID
    source_parse_fingerprint: str
    chunker_name: str
    chunker_version: int
    chunks: tuple[Chunk, ...]

    def __post_init__(self) -> None:
        if _chunker_specs().get(self.chunker_name) != self.chunker_version:
            raise ChunkingContractViolation("chunker_name/version 必须匹配已注册的 chunker")
        if (
            not isinstance(self.source_parse_fingerprint, str)
            or len(self.source_parse_fingerprint) != 64
        ):
            raise ChunkingContractViolation("source_parse_fingerprint 必须是 64 位 hex")
        if not isinstance(self.chunks, tuple) or not self.chunks:
            raise ChunkingContractViolation("chunks 必须是非空 tuple")
        for index, chunk in enumerate(self.chunks, start=1):
            if chunk.ordinal != index:
                raise ChunkingContractViolation("chunks 的 ordinal 必须连续 1..n")


def compute_chunk_set_fingerprint(document: ChunkSetDocument) -> str:
    """确定性 SHA-256 指纹（sort_keys + 固定 separators + UTF-8）。

    至少覆盖：parsed_source_id、source_parse_fingerprint、
    chunker_name/version、ordered chunks（text + locator_refs）。
    **不得包含** chunk_set_id / created_at / DB ID，禁止 repr() / hash()。

    同一 ParsedSource + 相同 chunks → 同一指纹。
    """
    payload = {
        "parsed_source_id": str(document.parsed_source_id),
        "source_parse_fingerprint": document.source_parse_fingerprint,
        "chunker_name": document.chunker_name,
        "chunker_version": document.chunker_version,
        "chunks": [
            {
                "ordinal": chunk.ordinal,
                "text": chunk.text,
                "text_sha256": chunk.text_sha256,
                "char_count": chunk.char_count,
                "locator_refs": [
                    {
                        "block_ordinal": ref.block_ordinal,
                        "char_start": ref.char_start,
                        "char_end": ref.char_end,
                        "locator": ref.locator,
                    }
                    for ref in chunk.locator_refs
                ],
            }
            for chunk in document.chunks
        ],
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
