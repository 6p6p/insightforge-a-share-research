"""Chunking contracts + fingerprint unit tests (stage 3A).

校验契约 dataclass 的输入防御、chunk_set_fingerprint 的确定性 / 敏感性 /
版本敏感性，以及 fingerprint 对 DB ID / 时间戳的排除语义。
"""

import hashlib
import uuid
from uuid import UUID

import pytest

from app.chunking.contracts import (
    CHUNKER_NAME,
    CHUNKER_VERSION,
    Chunk,
    ChunkBlockRef,
    ChunkSetDocument,
    compute_chunk_set_fingerprint,
)
from app.chunking.errors import ChunkingContractViolation

_PS_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_PS_ID_2 = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_FP = "b" * 64


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _locator(ordinal: int = 1) -> dict:
    return {
        "type": "html_dom",
        "ordinal": ordinal,
        "tag": "p",
        "xpath": "/html/body/article/p[1]",
        "element_id": None,
    }


def _ref(ordinal: int = 1, char_start: int = 0, char_end: int = 5, locator=None) -> ChunkBlockRef:
    return ChunkBlockRef(
        block_ordinal=ordinal,
        char_start=char_start,
        char_end=char_end,
        locator=locator if locator is not None else _locator(ordinal),
    )


def _chunk(ordinal: int = 1, text: str = "hello", refs=None) -> Chunk:
    refs = refs if refs is not None else (_ref(ordinal),)
    return Chunk(
        ordinal=ordinal,
        text=text,
        text_sha256=_sha(text),
        char_count=len(text),
        locator_refs=refs,
    )


def _document(
    *,
    parsed_source_id: UUID = _PS_ID,
    fingerprint: str = _FP,
    chunker_name: str = CHUNKER_NAME,
    chunker_version: int = CHUNKER_VERSION,
    chunks=None,
) -> ChunkSetDocument:
    chunks = chunks if chunks is not None else (_chunk(),)
    return ChunkSetDocument(
        parsed_source_id=parsed_source_id,
        source_parse_fingerprint=fingerprint,
        chunker_name=chunker_name,
        chunker_version=chunker_version,
        chunks=chunks,
    )


# ---------------------------------------------------------------- ChunkBlockRef


def test_chunk_block_ref_validates_positive_ordinal() -> None:
    with pytest.raises(ChunkingContractViolation):
        _ref(ordinal=0)
    with pytest.raises(ChunkingContractViolation):
        _ref(ordinal=-1)


def test_chunk_block_ref_validates_char_range() -> None:
    with pytest.raises(ChunkingContractViolation):
        _ref(char_start=-1, char_end=5)
    with pytest.raises(ChunkingContractViolation):
        _ref(char_start=5, char_end=5)  # end <= start 非法


def test_chunk_block_ref_requires_locator_dict() -> None:
    with pytest.raises(ChunkingContractViolation):
        _ref(locator="not-a-dict")


# ---------------------------------------------------------------- Chunk


def test_chunk_validates_text_hash() -> None:
    with pytest.raises(ChunkingContractViolation):
        Chunk(ordinal=1, text="abc", text_sha256="x" * 64, char_count=3, locator_refs=(_ref(),))


def test_chunk_validates_char_count() -> None:
    with pytest.raises(ChunkingContractViolation):
        Chunk(
            ordinal=1,
            text="abc",
            text_sha256=_sha("abc"),
            char_count=99,
            locator_refs=(_ref(),),
        )


def test_chunk_rejects_empty_text() -> None:
    with pytest.raises(ChunkingContractViolation):
        _chunk(text="")


def test_chunk_requires_at_least_one_ref() -> None:
    with pytest.raises(ChunkingContractViolation):
        Chunk(ordinal=1, text="abc", text_sha256=_sha("abc"), char_count=3, locator_refs=())


# ---------------------------------------------------------------- ChunkSetDocument


def test_document_validates_chunker_identity() -> None:
    with pytest.raises(ChunkingContractViolation):
        _document(chunker_name="other")
    with pytest.raises(ChunkingContractViolation):
        _document(chunker_version=2)


def test_document_validates_fingerprint_hex() -> None:
    with pytest.raises(ChunkingContractViolation):
        _document(fingerprint="not-hex")
    with pytest.raises(ChunkingContractViolation):
        _document(fingerprint="c" * 63)  # 长度不足 64


def test_document_requires_chunks() -> None:
    with pytest.raises(ChunkingContractViolation):
        _document(chunks=())


def test_document_requires_contiguous_ordinals() -> None:
    with pytest.raises(ChunkingContractViolation):
        _document(chunks=(_chunk(ordinal=1), _chunk(ordinal=3)))


# ---------------------------------------------------------------- fingerprint


def test_fingerprint_deterministic_across_runs() -> None:
    d1 = _document(chunks=(_chunk(text="甲" * 200), _chunk(text="乙" * 200, ordinal=2)))
    d2 = _document(chunks=(_chunk(text="甲" * 200), _chunk(text="乙" * 200, ordinal=2)))
    assert compute_chunk_set_fingerprint(d1) == compute_chunk_set_fingerprint(d2)
    assert len(compute_chunk_set_fingerprint(d1)) == 64


def test_fingerprint_sensitive_to_text() -> None:
    d1 = _document(chunks=(_chunk(text="甲" * 200), _chunk(text="乙" * 200, ordinal=2)))
    d2 = _document(chunks=(_chunk(text="甲" * 200), _chunk(text="乙" * 201, ordinal=2)))
    assert compute_chunk_set_fingerprint(d1) != compute_chunk_set_fingerprint(d2)


def test_fingerprint_sensitive_to_parsed_source() -> None:
    d1 = _document(chunks=(_chunk(),))
    d2 = _document(parsed_source_id=_PS_ID_2, chunks=(_chunk(),))
    assert compute_chunk_set_fingerprint(d1) != compute_chunk_set_fingerprint(d2)


def test_fingerprint_sensitive_to_parse_fingerprint() -> None:
    d1 = _document(chunks=(_chunk(),))
    d2 = _document(fingerprint="c" * 64, chunks=(_chunk(),))
    assert compute_chunk_set_fingerprint(d1) != compute_chunk_set_fingerprint(d2)


def test_fingerprint_sensitive_to_locator_refs() -> None:
    ref_a = _ref(ordinal=1)
    ref_b = _ref(ordinal=1, char_start=0, char_end=5, locator=_locator(ordinal=1))
    # 改变 locator 内容（xpath 不同）→ 指纹变化
    ref_c = _ref(
        ordinal=1,
        locator={
            "type": "html_dom",
            "ordinal": 1,
            "tag": "p",
            "xpath": "/html/body/article/p[2]",  # 不同 xpath
            "element_id": None,
        },
    )
    assert ref_a.locator != ref_c.locator
    d1 = _document(chunks=(_chunk(refs=(ref_a,)),))
    d2 = _document(chunks=(_chunk(refs=(ref_c,)),))
    assert compute_chunk_set_fingerprint(d1) != compute_chunk_set_fingerprint(d2)
    # 键序不影响（dict 相等 + sort_keys 序列化）
    d3 = _document(chunks=(_chunk(refs=(ref_b,)),))
    assert compute_chunk_set_fingerprint(d1) == compute_chunk_set_fingerprint(d3)


def test_fingerprint_excludes_db_ids_and_timestamps() -> None:
    """指纹只含 parsed_source_id + parse_fingerprint + chunker 身份 + chunks。

    不包含任何 chunk_set_id / chunk_id / created_at（这些字段不在契约结构中，
    且契约结构中无时间戳字段 —— 直接验证契约不承载它们）。
    """
    document = _document(chunks=(_chunk(),))
    # 契约 dataclass 没有 DB id / created_at 字段：
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(ChunkSetDocument)}
    assert "chunk_set_id" not in field_names
    assert "created_at" not in field_names
    for _ in document.chunks:
        assert "chunk_id" not in {f.name for f in dataclasses.fields(Chunk)}
        assert "created_at" not in {f.name for f in dataclasses.fields(Chunk)}
    # uuid4 之类随机值不会出现在序列化 payload 中
    assert str(uuid.uuid4()) not in compute_chunk_set_fingerprint(document)
