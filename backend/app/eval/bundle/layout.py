"""Frozen Evaluation Bundle 目录约定（stage 7B.1.1A）。

所有路径由固定 layout 派生；caller 只传 identity（case_id / version / fingerprint /
artifact_type），不能传任意 relative/absolute path。identity 一律做安全 segment
校验（拒绝路径分隔符 / `.` / `..`），防 path traversal。

bundle_root 是 local absolute path，**永远不进入 semantic fingerprint**。
"""

from pathlib import Path

from app.eval.contracts import StructuredArtifactType
from app.eval.errors import EvalContractError

_MANIFEST_FILENAME = "manifest.json"
CASES_DIR = "cases"
LABELS_DIR = "labels"
SNAPSHOTS_DIR = "snapshots"
_BLOBS_DIR = ("blobs", "sha256")
MACRO_DIR = "macro"
STRUCTURED_DIR = "structured"


def _check_segment(value: str, kind: str) -> str:
    """identity 作为单一路径段必须安全（不含分隔符 / 非 `.`/`..` / 非空）。"""
    if not value:
        raise EvalContractError(f"{kind} 不能为空")
    if "/" in value or "\\" in value:
        raise EvalContractError(f"{kind} 含非法路径分隔符")
    if value in (".", ".."):
        raise EvalContractError(f"{kind} 不能为相对路径段")
    return value


def manifest_path(root: Path) -> Path:
    return root / _MANIFEST_FILENAME


def case_path(root: Path, case_id: str, case_version: int) -> Path:
    _check_segment(case_id, "case_id")
    return root / CASES_DIR / f"{case_id}.v{case_version}.json"


def label_path(root: Path, case_id: str, case_version: int, label_version: int) -> Path:
    _check_segment(case_id, "case_id")
    return root / LABELS_DIR / f"{case_id}.v{case_version}.label{label_version}.json"


def snapshot_path(root: Path, snapshot_fingerprint: str) -> Path:
    _check_segment(snapshot_fingerprint, "snapshot_fingerprint")
    return root / SNAPSHOTS_DIR / f"{snapshot_fingerprint}.json"


def document_blob_path(root: Path, content_sha256: str) -> Path:
    _check_segment(content_sha256, "content_sha256")
    # content-address sharding：按前 2 位 hex 分桶，避免单目录海量文件。
    return root / _BLOBS_DIR[0] / _BLOBS_DIR[1] / content_sha256[:2] / content_sha256


def macro_payload_path(root: Path, snapshot_fingerprint: str) -> Path:
    _check_segment(snapshot_fingerprint, "snapshot_fingerprint")
    return root / MACRO_DIR / f"{snapshot_fingerprint}.json"


def structured_payload_path(
    root: Path, artifact_type: StructuredArtifactType, artifact_fingerprint: str
) -> Path:
    _check_segment(artifact_fingerprint, "artifact_fingerprint")
    return root / STRUCTURED_DIR / artifact_type.value / f"{artifact_fingerprint}.json"
