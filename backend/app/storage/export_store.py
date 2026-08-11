"""Content-addressed immutable export bytes on the local filesystem (stage 6C).

存储布局：`<root>/sha256/<ab>/<cd>/<64位sha256>.<ext>`（ext = md / docx / pdf）。

语义与 `LocalRawArtifactStore` 一致：
- 文件一旦写入内容寻址路径后不可覆盖；相同 SHA-256 内容只保留一份
  （并发渲染出相同字节 → 复用同一文件，`newly_created=False`）；
- `put_bytes` 先写随机临时文件 + flush + fsync → 原子 `os.replace` 到最终路径，
  失败即清理临时文件；
- `open` / `exists` 走 `_resolve` 路径守卫：绝对路径 / `..` / `.` / 空段一律拒绝
  （调用方无法用 caller-controlled path 逃逸根目录，spec L）；
- 任何日志不输出绝对路径。

`ExportArtifactStore` 不依赖 DB / 不调用 LLM / Retrieval / Chroma / Web——
只做字节内容寻址归档。
"""

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from app.report_export.errors import ExportArtifactNotFound, ExportStorageUnavailable

_CHUNK_SIZE = 1024 * 1024

# 允许的扩展名白名单（与 export_format 对应；拒绝任意 caller 扩展名）。
_ALLOWED_EXTENSIONS = {"md", "docx", "pdf"}


class InvalidStorageKey(Exception):
    """Internal guard: storage_key must stay within the store root."""


@dataclass(frozen=True)
class StoredExport:
    content_sha256: str
    storage_key: str
    byte_size: int
    newly_created: bool


class ExportArtifactStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def put_bytes(self, data: bytes, extension: str) -> StoredExport:
        """Store export bytes under the content-addressed path.

        相同 SHA → 复用（newly_created=False）；否则原子写入（newly_created=True）。
        空字节拒绝（渲染器不应产出空文件，且导出必非空）。
        """
        extension = extension.lower()
        if extension not in _ALLOWED_EXTENSIONS:
            raise InvalidStorageKey()
        if not data:
            raise ExportArtifactNotFound()
        self._root.mkdir(parents=True, exist_ok=True)
        tmp_dir = self._root / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        content_sha256 = hashlib.sha256(data).hexdigest()
        storage_key = self._storage_key_for(content_sha256, extension)
        final_path = self._resolve(storage_key)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        if final_path.exists():
            return StoredExport(
                content_sha256=content_sha256,
                storage_key=storage_key,
                byte_size=len(data),
                newly_created=False,
            )
        fd, tmp_path = tempfile.mkstemp(dir=tmp_dir, prefix="export-")
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp_path, final_path)
            return StoredExport(
                content_sha256=content_sha256,
                storage_key=storage_key,
                byte_size=len(data),
                newly_created=True,
            )
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def open(self, storage_key: str) -> BinaryIO:
        """Open a stored export file for reading; never exposes absolute host paths."""
        try:
            path = self._resolve(storage_key)
        except InvalidStorageKey:
            raise ExportArtifactNotFound() from None
        if not path.is_file():
            raise ExportArtifactNotFound()
        return path.open("rb")

    def exists(self, storage_key: str) -> bool:
        try:
            path = self._resolve(storage_key)
        except InvalidStorageKey:
            return False
        return path.is_file()

    def check_ready(self) -> None:
        """Verify the store is writable by creating and fsyncing a probe file.

        任一步（创建、写入、flush、fsync、删除探测文件）失败都视为存储不可用，
        抛 ExportStorageUnavailable；finally 中仍尽力清理，清理失败不再向外抛，
        也不把绝对路径带进错误。
        """
        probe_path: str | None = None
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            fd, probe_path = tempfile.mkstemp(dir=self._root, prefix=".export-ready-")
            try:
                with os.fdopen(fd, "wb") as probe:
                    probe.write(os.urandom(16))
                    probe.flush()
                    os.fsync(probe.fileno())
            except OSError:
                raise
            else:
                os.unlink(probe_path)
                probe_path = None
        except OSError as exc:
            raise ExportStorageUnavailable() from exc
        finally:
            if probe_path is not None:
                try:
                    os.unlink(probe_path)
                except OSError:
                    pass

    @staticmethod
    def _storage_key_for(content_sha256: str, extension: str) -> str:
        return f"sha256/{content_sha256[:2]}/{content_sha256[2:4]}/{content_sha256}.{extension}"

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
