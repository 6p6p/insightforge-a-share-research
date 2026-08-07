"""block_window chunker unit tests (stage 3A).

全部为确定性纯函数测试（不联网、不访问 DB），覆盖：
- 单 block / 多 block 合并（"\n" 分隔、target/max 边界）；
- oversized block 句末标点切分（中英文标点）+ hard split；
- 重复文本保留；中文 char 计数；locator_refs（char_start/char_end 相对
  原 block.text 的 Python [start, end) 语义）；
- HTML DOM / PDF page locator 保留；
- 不跨 ParsedSource；blocks 非空 / ordinal 连续校验；
- chunk ordinal 连续 1..n；
- 本阶段无 Chroma 依赖（源码级 guard）。
"""

import hashlib
from uuid import UUID

import pytest

from app.chunking.chunker import MAX_CHARS, TARGET_CHARS, chunk_parsed_document
from app.chunking.contracts import ChunkSetDocument
from app.chunking.errors import ChunkingContractViolation
from app.domain.parsing import ParsedBlockType
from app.parsing.contracts import ParsedBlock

_PS_ID = UUID("11111111-1111-1111-1111-111111111111")
_PS_ID_2 = UUID("22222222-2222-2222-2222-222222222222")
_FP = "a" * 64


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _html_block(ordinal: int, text: str, tag: str = "p") -> ParsedBlock:
    return ParsedBlock(
        ordinal=ordinal,
        block_type=ParsedBlockType.PARAGRAPH,
        text=text,
        text_sha256=_sha(text),
        locator={
            "type": "html_dom",
            "ordinal": ordinal,
            "tag": tag,
            "xpath": f"/html/body/article/{tag}[{ordinal}]",
            "element_id": None,
        },
    )


def _pdf_block(
    ordinal: int,
    text: str,
    *,
    page_number: int = 1,
    line_index: int | None = None,
) -> ParsedBlock:
    return ParsedBlock(
        ordinal=ordinal,
        block_type=ParsedBlockType.PARAGRAPH,
        text=text,
        text_sha256=_sha(text),
        locator={
            "type": "pdf_page",
            "page_number": page_number,
            "line_index": line_index if line_index is not None else ordinal,
            "bbox": [10.0, 20.0, 50.0, 30.0],
            "page_width": 612.0,
            "page_height": 792.0,
        },
    )


def _chunk(blocks: tuple[ParsedBlock, ...]) -> ChunkSetDocument:
    return chunk_parsed_document(_PS_ID, _FP, blocks)


def _refs_as_json(chunk) -> list[dict]:
    return [
        {
            "block_ordinal": ref.block_ordinal,
            "char_start": ref.char_start,
            "char_end": ref.char_end,
            "locator": ref.locator,
        }
        for ref in chunk.locator_refs
    ]


# ---------------------------------------------------------------- 基本分块


def test_single_small_block_single_chunk() -> None:
    doc = _chunk((_html_block(1, "短段落"),))
    assert len(doc.chunks) == 1
    chunk = doc.chunks[0]
    assert chunk.ordinal == 1
    assert chunk.text == "短段落"
    assert chunk.char_count == len("短段落")
    assert chunk.text_sha256 == _sha("短段落")
    assert _refs_as_json(chunk) == [
        {
            "block_ordinal": 1,
            "char_start": 0,
            "char_end": len("短段落"),
            "locator": chunk.locator_refs[0].locator,
        }
    ]
    assert chunk.locator_refs[0].locator["type"] == "html_dom"


def test_multi_block_merge_with_newline_separator() -> None:
    b1, b2 = _html_block(1, "甲" * 200), _html_block(2, "乙" * 200)
    doc = _chunk((b1, b2))
    assert len(doc.chunks) == 1
    chunk = doc.chunks[0]
    assert chunk.text == ("甲" * 200) + "\n" + ("乙" * 200)
    assert chunk.char_count == 401
    assert len(chunk.locator_refs) == 2
    assert chunk.locator_refs[0].block_ordinal == 1
    assert chunk.locator_refs[1].block_ordinal == 2


def test_target_boundary_starts_new_chunk() -> None:
    """达到 target_chars 后，下一个 block 不再合并（自然结算该 chunk）。"""
    b1, b2 = _html_block(1, "甲" * TARGET_CHARS), _html_block(2, "乙" * 100)
    doc = _chunk((b1, b2))
    assert [c.char_count for c in doc.chunks] == [TARGET_CHARS, 100]
    assert doc.chunks[0].locator_refs[0].block_ordinal == 1
    assert doc.chunks[1].locator_refs[0].block_ordinal == 2


def test_max_boundary_merge_exact() -> None:
    """合并后恰好 == max_chars 仍允许合并。"""
    b1, b2 = _html_block(1, "甲" * 299), _html_block(2, "乙" * 200)
    doc = _chunk((b1, b2))
    assert len(doc.chunks) == 1
    assert doc.chunks[0].char_count == MAX_CHARS
    assert doc.chunks[0].text == ("甲" * 299) + "\n" + ("乙" * 200)


def test_max_boundary_merge_rejected() -> None:
    """合并后超过 max_chars 则拒绝合并，各自独立成 chunk。"""
    b1, b2 = _html_block(1, "甲" * 300), _html_block(2, "乙" * 200)
    doc = _chunk((b1, b2))
    assert [c.char_count for c in doc.chunks] == [300, 200]


# ---------------------------------------------------------------- oversized


def test_oversized_block_sentence_split_cn() -> None:
    text = ("句" * 480) + "。" + ("句" * 419)
    assert len(text) == 900
    doc = _chunk((_html_block(1, text),))
    assert [c.char_count for c in doc.chunks] == [481, 419]
    # 每段对应原 block.text 的 [start, end) 字符索引
    seg1, seg2 = doc.chunks[0], doc.chunks[1]
    assert (seg1.locator_refs[0].char_start, seg1.locator_refs[0].char_end) == (0, 481)
    assert (seg2.locator_refs[0].char_start, seg2.locator_refs[0].char_end) == (481, 900)
    assert text[480] == "。"  # 句末标点属于前一段
    assert text[seg1.locator_refs[0].char_start : seg1.locator_refs[0].char_end] == text[:481]
    assert text[seg2.locator_refs[0].char_start : seg2.locator_refs[0].char_end] == text[481:900]


def test_oversized_block_sentence_split_english() -> None:
    # v1 句末标点集合 = 。！？!?；;（不含英文句点 "."）。英文问号在集合内。
    text = "sentence?" * 100  # 每句 9 字符
    assert len(text) == 900
    doc = _chunk((_html_block(1, text),))
    # 切分点落在句末标点（?）后：500 内最近的可切标点。
    sizes = [c.char_count for c in doc.chunks]
    assert sum(sizes) == 900
    for chunk in doc.chunks:
        assert chunk.char_count <= MAX_CHARS
    # 每段都以句末标点结尾
    assert all(c.text.endswith("?") for c in doc.chunks)
    # refs 覆盖整个 block 文本且不重叠
    spans = [
        (chunk.locator_refs[0].char_start, chunk.locator_refs[0].char_end) for chunk in doc.chunks
    ]
    assert spans[0][0] == 0 and spans[-1][1] == 900
    assert all(spans[i][1] == spans[i + 1][0] for i in range(len(spans) - 1))


def test_oversized_english_dot_falls_back_to_hard_split() -> None:
    """英文句点 '.' 不在 v1 句末标点集合 → 对英文句点文本走 hard split。"""
    text = "sentence." * 100
    assert len(text) == 900
    doc = _chunk((_html_block(1, text),))
    assert [c.char_count for c in doc.chunks] == [500, 400]
    # hard split 段不以句号结尾（切点与标点无关）
    assert not doc.chunks[0].text.endswith(".")


def test_oversized_block_hard_split() -> None:
    """无可用句末标点 → 确定性 hard split。"""
    doc = _chunk((_html_block(1, "x" * 1200),))
    assert [c.char_count for c in doc.chunks] == [500, 500, 200]
    # refs 仍指向原 block 文本的对应片段
    for chunk in doc.chunks:
        ref = chunk.locator_refs[0]
        assert chunk.text == ("x" * (ref.char_end - ref.char_start))


def test_oversized_block_followed_by_small_block_merges() -> None:
    """oversized 的最后一段（< target）可与后续 block 合并。"""
    big = _html_block(1, "x" * 600)  # hard split -> [500, 100]
    small = _html_block(2, "y" * 50)
    doc = _chunk((big, small))
    assert [c.char_count for c in doc.chunks] == [500, 151]
    assert doc.chunks[1].text == ("x" * 100) + "\n" + ("y" * 50)
    assert len(doc.chunks[1].locator_refs) == 2
    assert doc.chunks[1].locator_refs[0].block_ordinal == 1
    assert doc.chunks[1].locator_refs[0].char_start == 500
    assert doc.chunks[1].locator_refs[0].char_end == 600
    assert doc.chunks[1].locator_refs[1].block_ordinal == 2


def test_oversized_uses_cn_punctuation_set() -> None:
    text = ("甲" * 490) + "；" + ("甲" * 490)  # 中文分号也是句末标点
    assert len(text) == 981
    doc = _chunk((_html_block(1, text),))
    assert [c.char_count for c in doc.chunks] == [491, 490]
    assert text[490] == "；"


# ---------------------------------------------------------------- 语义保证


def test_duplicate_text_preserved() -> None:
    b1, b2 = _html_block(1, "同样的句子"), _html_block(2, "同样的句子")
    doc = _chunk((b1, b2))
    assert doc.chunks[0].text.count("同样的句子") == 2
    assert len(doc.chunks[0].locator_refs) == 2


def test_chinese_char_count_is_python_str_len() -> None:
    text = "中文段落内容。这里包含全角标点：" + "。" * 30
    doc = _chunk((_html_block(1, text),))
    assert doc.chunks[0].char_count == len(text)
    assert doc.chunks[0].char_count == len(list(text))


def test_html_dom_locator_preserved_in_refs() -> None:
    b1 = _html_block(1, "甲" * 300, tag="p")
    b2 = _html_block(2, "乙" * 100, tag="p")
    doc = _chunk((b1, b2))
    assert doc.chunks[0].locator_refs[0].locator == b1.locator
    assert doc.chunks[0].locator_refs[1].locator == b2.locator
    assert doc.chunks[0].locator_refs[0].locator["type"] == "html_dom"
    assert doc.chunks[0].locator_refs[0].locator["xpath"].startswith("/")


def test_pdf_page_locator_preserved_in_refs() -> None:
    b1 = _pdf_block(1, "第一行", page_number=2, line_index=7)
    doc = _chunk((b1,))
    ref = doc.chunks[0].locator_refs[0]
    assert ref.locator == b1.locator
    assert ref.locator["type"] == "pdf_page"
    assert ref.locator["page_number"] == 2
    assert ref.locator["line_index"] == 7
    assert ref.locator["bbox"] == [10.0, 20.0, 50.0, 30.0]


def test_chunks_do_not_cross_parsed_source() -> None:
    doc1 = chunk_parsed_document(_PS_ID, _FP, (_html_block(1, "甲" * 200),))
    doc2 = chunk_parsed_document(_PS_ID_2, _FP, (_html_block(1, "甲" * 200),))
    assert doc1.parsed_source_id == _PS_ID
    assert doc2.parsed_source_id == _PS_ID_2
    # 相同 blocks、不同 parsed_source → 各自独立（不跨 ParsedSource）。
    assert doc1.chunks == doc2.chunks  # chunks 内容一致（同一文本）
    assert doc1.parsed_source_id != doc2.parsed_source_id


def test_chunk_ordinal_contiguous() -> None:
    doc = _chunk((_html_block(1, "x" * 300), _html_block(2, "y" * 300)))
    assert [c.ordinal for c in doc.chunks] == list(range(1, len(doc.chunks) + 1))


def test_all_chunks_text_nonempty() -> None:
    doc = _chunk((_html_block(1, "x" * 1200),))
    for chunk in doc.chunks:
        assert chunk.text.strip() != ""


# ---------------------------------------------------------------- 校验路径


def test_empty_blocks_raises() -> None:
    with pytest.raises(ChunkingContractViolation):
        _chunk(())


def test_non_contiguous_ordinal_raises() -> None:
    with pytest.raises(ChunkingContractViolation):
        _chunk((_html_block(1, "x"), _html_block(3, "y")))


def test_no_chroma_dependency_in_chunking_modules() -> None:
    """Stage 3A 不得创建 Chroma collection / 引入 chromadb。

    检查 chunking 包全部子模块与 ChunkingService 的模块级命名空间：
    若有任何 `import chromadb` / `from chromadb import ...` 绑定，
    vars(module) 中会出现 chroma 相关名字。docstring / 注释里的说明性
    文字不影响本检查。
    """
    import importlib

    from app import chunking as chunking_pkg
    from app.services import chunking_service

    modules = [chunking_pkg, chunking_service]
    for submodule_name in ("chunker", "contracts", "errors"):
        modules.append(importlib.import_module(f"app.chunking.{submodule_name}"))
    for module in modules:
        bound = {name for name in vars(module) if "chroma" in name.lower()}
        assert not bound, f"{module.__name__} 绑定了 chroma 名字: {sorted(bound)}"
