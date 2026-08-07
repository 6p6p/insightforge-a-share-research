"""Tests for LocalRawArtifactStore: content-addressed immutable PDF storage."""

import hashlib
import io
import os
from pathlib import Path

import pytest

from app.core.errors import (
    InvalidPdfFile,
    RawArtifactNotFound,
    SourceFileTooLarge,
    SourceStorageUnavailable,
)
from app.storage.raw_store import LocalRawArtifactStore

_PDF = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"
_OTHER_PDF = b"%PDF-1.7\n%% This is another pdf.\n%%EOF\n"


def _store(root: Path, max_bytes: int = 1024 * 1024) -> LocalRawArtifactStore:
    return LocalRawArtifactStore(root=root, max_bytes=max_bytes)


def _expected_key(content: bytes) -> str:
    digest = hashlib.sha256(content).hexdigest()
    return f"sha256/{digest[:2]}/{digest[2:4]}/{digest}.pdf"


def test_put_stores_and_returns_metadata(tmp_path) -> None:
    store = _store(tmp_path / "raw")
    stored = store.put_pdf_stream(io.BytesIO(_PDF))

    assert stored.content_sha256 == hashlib.sha256(_PDF).hexdigest()
    assert stored.byte_size == len(_PDF)
    assert stored.media_type == "application/pdf"
    assert stored.newly_created is True
    assert stored.storage_key == _expected_key(_PDF)
    assert store.exists(stored.storage_key)


def test_put_storage_layout_is_content_addressed(tmp_path) -> None:
    store = _store(tmp_path / "raw")
    stored = store.put_pdf_stream(io.BytesIO(_PDF))
    path = store._resolve(stored.storage_key)
    assert path.is_file()
    assert path.read_bytes() == _PDF
    # sha256/ab/cd/<64位hash>.pdf
    relative = path.relative_to((tmp_path / "raw").resolve())
    assert len(relative.parts) == 4
    assert relative.parts[0] == "sha256"
    assert stored.storage_key.endswith(f"{stored.content_sha256}.pdf")


def test_same_content_deduplicates_to_single_file(tmp_path) -> None:
    store = _store(tmp_path / "raw")
    first = store.put_pdf_stream(io.BytesIO(_PDF))
    second = store.put_pdf_stream(io.BytesIO(_PDF))

    assert first.newly_created is True
    assert second.newly_created is False
    assert second.storage_key == first.storage_key
    # 内容寻址路径只有一份文件
    files = list((tmp_path / "raw").rglob("*.pdf"))
    assert len(files) == 1
    assert files[0].read_bytes() == _PDF


def test_different_content_creates_distinct_artifacts(tmp_path) -> None:
    store = _store(tmp_path / "raw")
    first = store.put_pdf_stream(io.BytesIO(_PDF))
    second = store.put_pdf_stream(io.BytesIO(_OTHER_PDF))

    assert first.storage_key != second.storage_key
    assert first.content_sha256 != second.content_sha256
    assert store.open(first.storage_key).read() == _PDF
    assert store.open(second.storage_key).read() == _OTHER_PDF


def test_rejects_non_pdf(tmp_path) -> None:
    store = _store(tmp_path / "raw")
    with pytest.raises(InvalidPdfFile):
        store.put_pdf_stream(io.BytesIO(b"not a pdf at all"))
    # 失败后不应残留任何文件
    assert list((tmp_path / "raw").rglob("*.pdf")) == []
    assert list((tmp_path / "raw" / "tmp").iterdir()) == []


def test_accepts_pdf_with_leading_ascii_whitespace(tmp_path) -> None:
    store = _store(tmp_path / "raw")
    content = b"\n\r\t %PDF-1.7\n%%EOF\n"
    stored = store.put_pdf_stream(io.BytesIO(content))
    assert stored.content_sha256 == hashlib.sha256(content).hexdigest()


def test_rejects_oversized_stream(tmp_path) -> None:
    store = _store(tmp_path / "raw", max_bytes=16)
    with pytest.raises(SourceFileTooLarge):
        store.put_pdf_stream(io.BytesIO(b"%PDF-1.7\n" + b"x" * 64))
    assert list((tmp_path / "raw" / "tmp").iterdir()) == []


def test_open_returns_exact_bytes(tmp_path) -> None:
    store = _store(tmp_path / "raw")
    stored = store.put_pdf_stream(io.BytesIO(_PDF))
    with store.open(stored.storage_key) as handle:
        assert handle.read() == _PDF


def test_open_missing_key_raises_not_found(tmp_path) -> None:
    store = _store(tmp_path / "raw")
    with pytest.raises(RawArtifactNotFound):
        store.open("sha256/aa/bb/" + "a" * 64 + ".pdf")


def test_open_rejects_path_traversal(tmp_path) -> None:
    store = _store(tmp_path / "raw")
    store.put_pdf_stream(io.BytesIO(_PDF))
    for evil in (
        "../secret.pdf",
        "sha256/../../secret.pdf",
        "..\\..\\secret.pdf",
        "/etc/passwd",
        "\\windows\\win.ini",
        "sha256/aa/./bb/x.pdf",
        "sha256/aa//bb/x.pdf",
        "",
    ):
        with pytest.raises(RawArtifactNotFound):
            store.open(evil)
        assert store.exists(evil) is False


def test_exists_flags_stored_files(tmp_path) -> None:
    store = _store(tmp_path / "raw")
    stored = store.put_pdf_stream(io.BytesIO(_PDF))
    assert store.exists(stored.storage_key) is True
    assert store.exists("sha256/zz/zz/" + "b" * 64 + ".pdf") is False


def test_existing_content_is_not_overwritten(tmp_path) -> None:
    store = _store(tmp_path / "raw")
    stored = store.put_pdf_stream(io.BytesIO(_PDF))
    final = store._resolve(stored.storage_key)
    # 篡改已归档文件
    final.write_bytes(b"%PDF-1.7\nTAMPERED\n%%EOF\n")
    # 再次归档相同内容：检测到存在即复用，不覆盖磁盘上的文件
    again = store.put_pdf_stream(io.BytesIO(_PDF))
    assert again.newly_created is False
    assert final.read_bytes() == b"%PDF-1.7\nTAMPERED\n%%EOF\n"


def test_check_ready_succeeds_on_writable_root(tmp_path) -> None:
    store = _store(tmp_path / "raw")
    store.check_ready()
    assert (tmp_path / "raw").is_dir()


def test_check_ready_raises_on_unwritable_root(tmp_path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"not a directory")
    store = _store(blocker)
    with pytest.raises(SourceStorageUnavailable):
        store.check_ready()


def test_check_ready_leaves_no_probe_file(tmp_path) -> None:
    store = _store(tmp_path / "raw")
    store.check_ready()
    leftovers = [p for p in (tmp_path / "raw").iterdir() if p.name.startswith(".ready-")]
    assert leftovers == []


def test_check_ready_fsync_failure_raises_unavailable(monkeypatch, tmp_path) -> None:
    store = _store(tmp_path / "raw")

    def fail_fsync(fd) -> None:
        raise OSError("fsync failed")

    monkeypatch.setattr(os, "fsync", fail_fsync)
    with pytest.raises(SourceStorageUnavailable):
        store.check_ready()
    # 失败路径也必须尽力清理，不残留探测文件
    leftovers = [p for p in (tmp_path / "raw").iterdir() if p.name.startswith(".ready-")]
    assert leftovers == []


def test_check_ready_delete_failure_does_not_leak_path(monkeypatch, tmp_path) -> None:
    store = _store(tmp_path / "raw")
    real_unlink = os.unlink

    def fail_unlink(path) -> None:
        if Path(path).name.startswith(".ready-"):
            raise OSError("unlink failed")
        return real_unlink(path)

    monkeypatch.setattr(os, "unlink", fail_unlink)
    # 删除失败被吞掉：check_ready 不抛异常，也不把绝对路径带上错误
    store.check_ready()
    assert (tmp_path / "raw").is_dir()
