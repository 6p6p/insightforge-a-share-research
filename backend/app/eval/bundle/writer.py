"""EvaluationBundleWriter（stage 7B.1.1A）。

只接收已经构造好的 frozen contracts，不查询 DB。所有写：
- atomic：temp file → `os.replace`，避免半写。
- same-path identity（case / label / manifest）：目标已存在时必须 full contract
  byte-identical 才 replay，否则 `EvalFingerprintError`——semantic fingerprint 故意
  排除的字段（case 的 human_label_fingerprint、label 的 annotation）也不允许被
  silent replay，避免「同一路径语义相同但契约不同」被悄悄当 replay。
- semantic-fingerprint path（snapshot）：path 由 snapshot semantic fingerprint 派生，
  provenance UUID-only 变化 = replay（有意政策，见 write_snapshot 注释）。
- payload（macro / structured / document blob）：content-address——写入前 canonical
  bytes 的 SHA256 必须等于 ref.payload_sha256 / content_sha256，绝不写未校验字节。
"""

import hashlib
from pathlib import Path
from typing import Any

from app.eval.bundle import _io, layout
from app.eval.canonical import canonical_json_bytes
from app.eval.contracts import (
    EvalCase,
    EvalDatasetManifest,
    FrozenMacroSnapshotRef,
    FrozenSourceSnapshot,
    FrozenStructuredArtifactRef,
    HumanLabel,
)
from app.eval.errors import EvalContractError, EvalFingerprintError
from app.eval.fingerprints import compute_source_snapshot_fingerprint


class EvaluationBundleWriter:
    """把 frozen contracts 组织成可复制、可校验、可重放的目录（不查 DB）。"""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    # ------------------------------------------------------------ contract JSON

    def write_manifest(self, manifest: EvalDatasetManifest) -> None:
        self._write_contract_exact(layout.manifest_path(self._root), manifest, "manifest")

    def write_case(self, case: EvalCase) -> None:
        self._write_contract_exact(
            layout.case_path(self._root, case.case_id, case.case_version),
            case,
            f"case:{case.case_id} v{case.case_version}",
        )

    def write_label(self, label: HumanLabel) -> None:
        self._write_contract_exact(
            layout.label_path(self._root, label.case_id, label.case_version, label.label_version),
            label,
            f"label:{label.case_id} v{label.case_version}.label{label.label_version}",
        )

    def write_snapshot(self, snapshot: FrozenSourceSnapshot) -> None:
        # snapshot path 由 semantic fingerprint 派生；semantic 一致但 provenance
        # UUID 不同 → replay（保留首次写入的 bytes，下游只信任 semantic fp）。这是
        # 有意政策：UUID 只是 provenance pointer，不决定 frozen input 身份。
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

    def write_macro_payload(self, ref: FrozenMacroSnapshotRef, payload: dict[str, Any]) -> None:
        if payload.get("snapshot_fingerprint") != ref.snapshot_fingerprint:
            raise EvalContractError("macro payload envelope snapshot_fingerprint 不匹配")
        path = layout.macro_payload_path(self._root, ref.snapshot_fingerprint)
        self._write_payload_sha(
            path, payload, ref.payload_sha256, f"macro:{ref.snapshot_fingerprint}"
        )

    def write_structured_payload(
        self, ref: FrozenStructuredArtifactRef, payload: dict[str, Any]
    ) -> None:
        if payload.get("artifact_type") != ref.artifact_type.value:
            raise EvalContractError("structured payload envelope artifact_type 不匹配")
        if payload.get("artifact_fingerprint") != ref.artifact_fingerprint:
            raise EvalContractError("structured payload envelope artifact_fingerprint 不匹配")
        path = layout.structured_payload_path(
            self._root, ref.artifact_type, ref.artifact_fingerprint
        )
        self._write_payload_sha(
            path,
            payload,
            ref.payload_sha256,
            f"structured:{ref.artifact_type.value}:{ref.artifact_fingerprint}",
        )

    # ------------------------------------------------------------------ internals

    def _write_contract(self, path: Path, obj: Any, model: Any, fp_fn: Any, kind: str) -> None:
        # snapshot 专用：replay 按 semantic fingerprint（UUID-only 变化 = replay）。
        data = canonical_json_bytes(obj.model_dump(mode="json")) + b"\n"
        if path.exists():
            existing = _io.read_json_model(path, model, kind)
            if fp_fn(existing) == fp_fn(obj):
                return
            raise EvalFingerprintError(f"{kind} 已存在且语义不一致，拒绝覆盖")
        _io.atomic_write_bytes(path, data)

    def _write_contract_exact(self, path: Path, obj: Any, kind: str) -> None:
        # same-path identity（case / label / manifest）：full contract 必须
        # byte-identical，否则 reject。semantic fingerprint 故意排除的字段也不允许
        # silent replay。
        data = canonical_json_bytes(obj.model_dump(mode="json")) + b"\n"
        if path.exists():
            existing = _io.read_raw_bytes(path, kind)
            if existing == data:
                return
            raise EvalFingerprintError(f"{kind} 已存在且 full contract 不同，拒绝覆盖")
        _io.atomic_write_bytes(path, data)

    def _write_payload_sha(
        self, path: Path, payload: dict[str, Any], expected_sha: str, kind: str
    ) -> None:
        # content-address：canonical bytes 的 SHA256 必须 == ref.payload_sha256。
        # 文件以 exact canonical bytes 存储（无尾随 newline），使 file SHA == payload_sha256。
        data = canonical_json_bytes(payload)
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected_sha:
            raise EvalFingerprintError(f"{kind} 内容 SHA 与 ref.payload_sha256 不匹配")
        if path.exists():
            existing = _io.read_raw_bytes(path, kind)
            if hashlib.sha256(existing).hexdigest() != expected_sha:
                raise EvalFingerprintError(f"{kind} 已存在但字节 SHA 不匹配")
            return
        _io.atomic_write_bytes(path, data)
