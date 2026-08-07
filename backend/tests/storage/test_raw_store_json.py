"""Tests for LocalRawArtifactStore JSON archive (stage 2C.2A)."""

import hashlib
import io
from pathlib import Path

import pytest

from app.core.errors import InvalidJsonFile, SourceFileTooLarge
from app.storage.raw_store import LocalRawArtifactStore

_JSON = (
    b'{"page": 1, "pages": 1, "per_page": 1000, "total": 1,'
    b' "rows": [{"indicator": {"id": "SP.POP.TOTL"}, "value": 123}]}'
)
_JSON_OTHER = b'{"page": 2, "pages": 2, "rows": [{"value": 456}]}'
_PDF = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"


def _store(root: Path, *, max_json_bytes: int = 1024 * 1024) -> LocalRawArtifactStore:
    return LocalRawArtifactStore(
        root=root,
        max_bytes=1024 * 1024,
        max_json_bytes=max_json_bytes,
    )


def _expected_json_key(content: bytes) -> str:
    digest = hashlib.sha256(content).hexdigest()
    return f"sha256/{digest[:2]}/{digest[2:4]}/{digest}.json"


def test_put_json_stores_and_returns_metadata(tmp_path) -> None:
    store = _store(tmp_path / "raw")
    stored = store.put_json_bytes(_JSON)

    assert stored.content_sha256 == hashlib.sha256(_JSON).hexdigest()
    assert stored.byte_size == len(_JSON)
    assert stored.media_type == "application/json"
    assert stored.newly_created is True
    assert stored.storage_key == _expected_json_key(_JSON)
    assert store.exists(stored.storage_key)


def test_put_json_layout_is_content_addressed(tmp_path) -> None:
    store = _store(tmp_path / "raw")
    stored = store.put_json_bytes(_JSON)
    path = store._resolve(stored.storage_key)
    assert path.is_file()
    assert path.read_bytes() == _JSON
    # sha256/ab/cd/<64位hash>.json
    relative = path.relative_to((tmp_path / "raw").resolve())
    assert len(relative.parts) == 4
    assert relative.parts[0] == "sha256"
    assert stored.storage_key.endswith(f"{stored.content_sha256}.json")


def test_same_json_content_deduplicates_to_single_file(tmp_path) -> None:
    store = _store(tmp_path / "raw")
    first = store.put_json_bytes(_JSON)
    second = store.put_json_bytes(_JSON)

    assert first.newly_created is True
    assert second.newly_created is False
    assert second.storage_key == first.storage_key
    files = list((tmp_path / "raw").rglob("*.json"))
    assert len(files) == 1
    assert files[0].read_bytes() == _JSON


def test_different_json_content_creates_distinct_artifacts(tmp_path) -> None:
    store = _store(tmp_path / "raw")
    first = store.put_json_bytes(_JSON)
    second = store.put_json_bytes(_JSON_OTHER)

    assert first.storage_key != second.storage_key
    assert first.content_sha256 != second.content_sha256
    assert store.open(first.storage_key).read() == _JSON
    assert store.open(second.storage_key).read() == _JSON_OTHER


def test_json_with_utf8_bom_accepted_and_stored_verbatim(tmp_path) -> None:
    store = _store(tmp_path / "raw")
    bom_content = b"\xef\xbb\xbf" + _JSON
    stored = store.put_json_bytes(bom_content)
    assert stored.content_sha256 == hashlib.sha256(bom_content).hexdigest()
    assert store.open(stored.storage_key).read() == bom_content


def test_json_empty_bytes_rejected(tmp_path) -> None:
    store = _store(tmp_path / "raw")
    with pytest.raises(InvalidJsonFile):
        store.put_json_bytes(b"")
    assert list((tmp_path / "raw" / "tmp").iterdir()) == []


def test_json_non_utf8_rejected(tmp_path) -> None:
    store = _store(tmp_path / "raw")
    # UTF-16 头 + JSON：只有 UTF-8（允许 BOM）是合法输入
    with pytest.raises(InvalidJsonFile):
        store.put_json_bytes(b'\xff\xfe{\x00"\x00a\x00:\x001\x00}\x00')


def test_json_malformed_rejected(tmp_path) -> None:
    store = _store(tmp_path / "raw")
    with pytest.raises(InvalidJsonFile):
        store.put_json_bytes(b'{"page": 1,')
    assert list((tmp_path / "raw" / "tmp").iterdir()) == []


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_json_non_finite_literals_rejected(tmp_path, literal: str) -> None:
    store = _store(tmp_path / "raw")
    with pytest.raises(InvalidJsonFile):
        store.put_json_bytes(f'{{"value": {literal}}}'.encode())


def test_json_oversized_rejected(tmp_path) -> None:
    store = _store(tmp_path / "raw", max_json_bytes=16)
    with pytest.raises(SourceFileTooLarge):
        store.put_json_bytes(b'{"value": ' + b"1" * 32 + b"}")
    assert list((tmp_path / "raw" / "tmp").iterdir()) == []


def test_json_oversize_checked_before_parsing(tmp_path) -> None:
    # 超过上限即使内容不合法也只报大小错误（大小检查先于 JSON 解析）
    store = _store(tmp_path / "raw", max_json_bytes=8)
    with pytest.raises(SourceFileTooLarge):
        store.put_json_bytes(b"not json at all but definitely over the limit")


def test_json_saves_original_bytes_verbatim(tmp_path) -> None:
    # 不重新序列化、不格式化、不改键序：读回必须与输入完全一致
    store = _store(tmp_path / "raw")
    weird_format = b'  {  "z" : 1, "a" : {"nested" : [1, 2,  3]}, "n": 1.2300 }'
    stored = store.put_json_bytes(weird_format)
    assert store.open(stored.storage_key).read() == weird_format


def test_json_large_precision_number_accepted(tmp_path) -> None:
    # 校验阶段使用 Decimal 解析，接受超 2^53 的大数而不丢精度（保存原始字节）
    store = _store(tmp_path / "raw")
    big = b'{"value": 123456789012345678901234567890.123456789}'
    stored = store.put_json_bytes(big)
    assert store.open(stored.storage_key).read() == big


def test_json_storage_key_derived_only_from_content(tmp_path) -> None:
    # 存储键完全由内容哈希决定，与任何外部文件名无关（API 不接收文件名）
    store = _store(tmp_path / "raw")
    a = store.put_json_bytes(_JSON)
    b = store.put_json_bytes(_JSON)
    assert a.storage_key == b.storage_key
    assert a.storage_key == _expected_json_key(_JSON)


def test_json_and_pdf_paths_do_not_collide(tmp_path) -> None:
    store = _store(tmp_path / "raw")
    json_stored = store.put_json_bytes(_JSON)
    pdf_stored = store.put_pdf_stream(io.BytesIO(_PDF))
    assert json_stored.storage_key != pdf_stored.storage_key
    assert json_stored.storage_key.endswith(".json")
    assert pdf_stored.storage_key.endswith(".pdf")
    assert store.open(json_stored.storage_key).read() == _JSON
    assert store.open(pdf_stored.storage_key).read() == _PDF


def test_open_json_returns_exact_bytes(tmp_path) -> None:
    store = _store(tmp_path / "raw")
    stored = store.put_json_bytes(_JSON)
    with store.open(stored.storage_key) as handle:
        assert handle.read() == _JSON


def test_exists_flags_stored_json(tmp_path) -> None:
    store = _store(tmp_path / "raw")
    stored = store.put_json_bytes(_JSON)
    assert store.exists(stored.storage_key) is True
    assert store.exists("sha256/zz/zz/" + "b" * 64 + ".json") is False


def test_json_reopenable_after_store_recreation(tmp_path) -> None:
    # "重启" 持久化：新实例指向同一 root 仍能打开既有 JSON 归档
    root = tmp_path / "raw"
    stored = _store(root).put_json_bytes(_JSON)
    reopened = _store(root)
    assert reopened.exists(stored.storage_key) is True
    with reopened.open(stored.storage_key) as handle:
        assert handle.read() == _JSON


def test_failed_json_write_leaves_no_tmp_files(tmp_path) -> None:
    store = _store(tmp_path / "raw")
    with pytest.raises(InvalidJsonFile):
        store.put_json_bytes(b"{broken")
    assert list((tmp_path / "raw" / "tmp").iterdir()) == []
    assert list((tmp_path / "raw").rglob("*.json")) == []


def test_json_error_message_has_no_absolute_path(tmp_path) -> None:
    store = _store(tmp_path / "raw")
    with pytest.raises(InvalidJsonFile) as excinfo:
        store.put_json_bytes(b"{broken")
    assert str(tmp_path) not in str(excinfo.value)
    assert "raw" not in str(excinfo.value).lower()
