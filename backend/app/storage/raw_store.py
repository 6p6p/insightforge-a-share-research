"""Content-addressed immutable raw artifact storage on the local filesystem.

存储布局：<root>/sha256/<ab>/<cd>/<64位hash>.pdf
文件一旦写入内容寻址路径后不可覆盖；相同 SHA-256 内容只保留一份。
"""

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from app.core.errors import (
    InvalidPdfFile,
    RawArtifactNotFound,
    SourceFileTooLarge,
    SourceStorageUnavailable,
)

_PDF_MAGIC = b"%PDF-"
_CHUNK_SIZE = 1024 * 1024
_HEAD_SCAN_BYTES = 1024
_ASCII_WHITESPACE = b"\t\n\r\x20"
_MEDIA_TYPE_PDF = "application/pdf"


class InvalidStorageKey(Exception):
    """Internal guard: storage_key must stay within the store root."""


@dataclass(frozen=True)
class StoredRawArtifact:
    content_sha256: str
    storage_key: str
    byte_size: int
    media_type: str
    newly_created: bool


class LocalRawArtifactStore:
    def __init__(self, root: Path, max_bytes: int) -> None:
        self._root = root
        self._max_bytes = max_bytes

    def put_pdf_stream(self, stream: BinaryIO) -> StoredRawArtifact:
        """Stream a PDF into the content-addressed store.

        过程：写临时文件并增量计算 SHA-256 → 超限即失败清理 → PDF 头校验 →
        已存在则复用（newly_created=False）→ 否则原子移动到内容寻址路径。
        """
        self._root.mkdir(parents=True, exist_ok=True)
        tmp_dir = self._root / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        hasher = hashlib.sha256()
        byte_size = 0
        fd, tmp_path = tempfile.mkstemp(dir=tmp_dir, prefix="upload-")
        try:
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = stream.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    byte_size += len(chunk)
                    if byte_size > self._max_bytes:
                        raise SourceFileTooLarge()
                    hasher.update(chunk)
                    out.write(chunk)
            content_sha256 = hasher.hexdigest()
            if not self._has_pdf_signature(tmp_path, byte_size):
                raise InvalidPdfFile()
            storage_key = self._storage_key_for(content_sha256)
            final_path = self._resolve(storage_key)
            final_path.parent.mkdir(parents=True, exist_ok=True)
            if final_path.exists():
                os.unlink(tmp_path)
                return StoredRawArtifact(
                    content_sha256=content_sha256,
                    storage_key=storage_key,
                    byte_size=byte_size,
                    media_type=_MEDIA_TYPE_PDF,
                    newly_created=False,
                )
            os.replace(tmp_path, final_path)
            return StoredRawArtifact(
                content_sha256=content_sha256,
                storage_key=storage_key,
                byte_size=byte_size,
                media_type=_MEDIA_TYPE_PDF,
                newly_created=True,
            )
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def open(self, storage_key: str) -> BinaryIO:
        """Open a stored file for reading; never exposes absolute host paths."""
        try:
            path = self._resolve(storage_key)
        except InvalidStorageKey:
            raise RawArtifactNotFound() from None
        if not path.is_file():
            raise RawArtifactNotFound()
        return path.open("rb")

    def exists(self, storage_key: str) -> bool:
        try:
            path = self._resolve(storage_key)
        except InvalidStorageKey:
            return False
        return path.is_file()

    def check_ready(self) -> None:
        """Verify the store is writable by creating and fsyncing a probe file."""
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            fd, probe_path = tempfile.mkstemp(dir=self._root, prefix=".ready-")
            try:
                with os.fdopen(fd, "wb") as probe:
                    # 写少量随机字节，避免使用固定内容（readiness 不产生 DB 记录）
                    probe.write(os.urandom(16))
                    probe.flush()
                    os.fsync(probe.fileno())
            finally:
                # 尽力清理探测文件；删除失败不向外泄露路径，直接吞掉
                try:
                    os.unlink(probe_path)
                except OSError:
                    pass
        except OSError as exc:
            raise SourceStorageUnavailable() from exc

    @staticmethod
    def _storage_key_for(content_sha256: str) -> str:
        return f"sha256/{content_sha256[:2]}/{content_sha256[2:4]}/{content_sha256}.pdf"

    def _resolve(self, storage_key: str) -> Path:
        if not storage_key or storage_key.startswith(("/", "\\")):
            raise InvalidStorageKey()
        segments = storage_key.split("/")
        if any(segment in ("", ".", "..") for segment in segments):
            raise InvalidStorageKey()
        root = self._root.resolve()
        candidate = (root / storage_key).resolve()
        if not candidate.is_relative_to(root):
            raise InvalidStorageKey()
        return candidate

    @staticmethod
    def _has_pdf_signature(path: Path, size: int) -> bool:
        with open(path, "rb") as handle:
            head = handle.read(min(size, _HEAD_SCAN_BYTES))
        return head.lstrip(_ASCII_WHITESPACE).startswith(_PDF_MAGIC)
