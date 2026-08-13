"""EvaluationBundleWriter（stage 7B.1.1A）。

只接收已经构造好的 frozen contracts，不查询 DB。所有写：
- atomic：temp file → `os.replace`，避免半写。
- content-address + replay：目标已存在时 load → validate → recompute semantic
  fingerprint；语义一致 → 无操作（replay）；不一致 → `EvalFingerprintError` /
  `EvalContractError`。绝不 silent overwrite 旧 frozen artifact。
"""

import hashlib
from pathlib import Path
from typing import Any

from app.eval.bundle import _io, layout
from app.eval.canonical import canonical_json_bytes
from app.eval.contracts import (
    EvalCase,
    EvalDatasetManifest,
    FrozenSourceSnapshot,
    HumanLabel,
    StructuredArtifactType,
)
from app.eval.errors import EvalContractError, EvalFingerprintError
from app.eval.fingerprints import (
    compute_dataset_fingerprint,
    compute_eval_case_fingerprint,
    compute_human_label_fingerprint,
    compute_source_snapshot_fingerprint,
)


class EvaluationBundleWriter:
    """把 frozen contracts 组织成可复制、可校验、可重放的目录（不查 DB）。"""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    # ------------------------------------------------------------ contract JSON

    def write_manifest(self, manifest: EvalDatasetManifest) -> None:
        self._write_contract(
            layout.manifest_path(self._root),
            manifest,
            EvalDatasetManifest,
            compute_dataset_fingerprint,
            "manifest",
        )

    def write_case(self, case: EvalCase) -> None:
        self._write_contract(
            layout.case_path(self._root, case.case_id, case.case_version),
            case,
            EvalCase,
            compute_eval_case_fingerprint,
            f"case:{case.case_id} v{case.case_version}",
        )

    def write_label(self, label: HumanLabel) -> None:
        self._write_contract(
            layout.label_path(self._root, label.case_id, label.case_version, label.label_version),
            label,
            HumanLabel,
            compute_human_label_fingerprint,
            f"label:{label.case_id} v{label.case_version}.label{label.label_version}",
        )

    def write_snapshot(self, snapshot: FrozenSourceSnapshot) -> None:
        fp = compute_source_snapshot_fingerprint(snapshot)
        self._write_contract(
            layout.snapshot_path(self._root, fp),
            snapshot,
            FrozenSourceSnapshot,
            compute_source_snapshot_fingerprint,
            f"snapshot:{fp}",
        )

    # ------------------------------------------------------ content-addressed blob

    def write_document_blob(self, content_sha256: str, content: bytes) -> None:
        actual = hashlib.sha256(content).hexdigest()
        if actual != content_sha256:
            raise EvalFingerprintError("document blob 内容 hash 与 content_sha256 不匹配")
        path = layout.document_blob_path(self._root, content_sha256)
        if path.exists():
            existing = _io.read_raw_bytes(path, f"document blob {content_sha256[:8]}...")
            if hashlib.sha256(existing).hexdigest() != content_sha256:
                raise EvalFingerprintError("document blob 已存在但内容不匹配（integrity）")
            return
        _io.atomic_write_bytes(path, content)

    # ------------------------------------------------------ macro / structured payload

    def write_macro_payload(self, snapshot_fingerprint: str, payload: dict[str, Any]) -> None:
        if payload.get("snapshot_fingerprint") != snapshot_fingerprint:
            raise EvalContractError("macro payload envelope snapshot_fingerprint 不匹配")
        path = layout.macro_payload_path(self._root, snapshot_fingerprint)
        self._write_payload(path, payload, f"macro:{snapshot_fingerprint}")

    def write_structured_payload(
        self,
        artifact_type: StructuredArtifactType,
        artifact_fingerprint: str,
        payload: dict[str, Any],
    ) -> None:
        if payload.get("artifact_type") != artifact_type.value:
            raise EvalContractError("structured payload envelope artifact_type 不匹配")
        if payload.get("artifact_fingerprint") != artifact_fingerprint:
            raise EvalContractError("structured payload envelope artifact_fingerprint 不匹配")
        path = layout.structured_payload_path(self._root, artifact_type, artifact_fingerprint)
        self._write_payload(
            path, payload, f"structured:{artifact_type.value}:{artifact_fingerprint}"
        )

    # ------------------------------------------------------------------ internals

    def _write_contract(self, path: Path, obj: Any, model: Any, fp_fn: Any, kind: str) -> None:
        data = canonical_json_bytes(obj.model_dump(mode="json")) + b"\n"
        if path.exists():
            existing = _io.read_json_model(path, model, kind)
            if fp_fn(existing) == fp_fn(obj):
                return
            raise EvalFingerprintError(f"{kind} 已存在且语义不一致，拒绝覆盖")
        _io.atomic_write_bytes(path, data)

    def _write_payload(self, path: Path, payload: dict[str, Any], kind: str) -> None:
        data = canonical_json_bytes(payload) + b"\n"
        if path.exists():
            existing = _io.read_json_dict(path, kind)
            if canonical_json_bytes(existing) == canonical_json_bytes(payload):
                return
            raise EvalFingerprintError(f"{kind} 已存在且内容不一致，拒绝覆盖")
        _io.atomic_write_bytes(path, data)
