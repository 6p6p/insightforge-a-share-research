"""EvaluationBundleLoader（stage 7B.1.1A）。

所有 path 通过固定 layout 派生；caller 只传 identity，不能传任意 relative/absolute
path。Human label 只能通过 `load_label` / `load_label_by_fingerprint` 单独读取；
`load_execution_case` 返回的 `LoadedEvalExecutionCase` **不含** HumanLabel /
human_label_fingerprint（label leakage boundary）。
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from app.eval.bundle import _io, layout
from app.eval.contracts import (
    EvalCase,
    EvalDatasetManifest,
    FrozenCompanyIdentity,
    FrozenSourceSnapshot,
    HumanLabel,
    StructuredArtifactType,
)
from app.eval.errors import EvalContractError
from app.eval.fingerprints import compute_eval_case_fingerprint, compute_human_label_fingerprint


@dataclass(frozen=True)
class LoadedEvalExecutionCase:
    """execution 侧加载结果：不含 HumanLabel / human_label_fingerprint / label path。

    `case_id` / `case_version` 是 execution runtime 归属输出所需的稳定语义身份
    （harness 用它校验 variant output 的 case identity），不是 label 信息。
    """

    case_fingerprint: str
    case_id: str
    case_version: int
    company_id: UUID
    company: FrozenCompanyIdentity
    research_question: str
    analysis_as_of: datetime
    tags: tuple[str, ...]
    snapshot: FrozenSourceSnapshot


class EvaluationBundleLoader:
    """从固定 layout 读取 bundle 内容；所有 path 由 identity 派生。"""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def load_manifest(self) -> EvalDatasetManifest:
        return _io.read_json_model(
            layout.manifest_path(self._root), EvalDatasetManifest, "manifest"
        )

    def load_case(self, case_id: str, case_version: int) -> EvalCase:
        path = layout.case_path(self._root, case_id, case_version)
        return _io.read_json_model(path, EvalCase, f"case:{case_id} v{case_version}")

    def load_snapshot(self, snapshot_fingerprint: str) -> FrozenSourceSnapshot:
        path = layout.snapshot_path(self._root, snapshot_fingerprint)
        return _io.read_json_model(path, FrozenSourceSnapshot, f"snapshot:{snapshot_fingerprint}")

    def load_label(self, case_id: str, case_version: int, label_version: int) -> HumanLabel:
        path = layout.label_path(self._root, case_id, case_version, label_version)
        return _io.read_json_model(
            path, HumanLabel, f"label:{case_id} v{case_version}.label{label_version}"
        )

    def load_label_by_fingerprint(
        self, case_id: str, case_version: int, label_fingerprint: str
    ) -> HumanLabel:
        """按 semantic fingerprint 找到唯一 label（case 只存 fingerprint 不存 label_version）。"""
        matches: list[HumanLabel] = []
        labels_dir = self._root / layout.LABELS_DIR
        prefix = f"{case_id}.v{case_version}.label"
        if labels_dir.is_dir():
            for p in sorted(labels_dir.iterdir()):
                if p.name.startswith(prefix) and p.name.endswith(".json"):
                    label = _io.read_json_model(p, HumanLabel, f"label:{p.name}")
                    if label.case_id == case_id and label.case_version == case_version:
                        if compute_human_label_fingerprint(label) == label_fingerprint:
                            matches.append(label)
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise EvalContractError(
                f"未找到匹配 label（case:{case_id} v{case_version}，fingerprint 不匹配）"
            )
        raise EvalContractError(f"找到多个匹配 label（case:{case_id} v{case_version}）")

    def read_document_blob(self, content_sha256: str) -> bytes:
        path = layout.document_blob_path(self._root, content_sha256)
        return _io.read_raw_bytes(path, f"document blob {content_sha256[:8]}...")

    def load_macro_payload(self, snapshot_fingerprint: str) -> dict[str, Any]:
        path = layout.macro_payload_path(self._root, snapshot_fingerprint)
        return _io.read_json_dict(path, f"macro:{snapshot_fingerprint}")

    def read_macro_payload_bytes(self, snapshot_fingerprint: str) -> bytes:
        """读取 macro payload 的原始 canonical bytes（供 payload_sha256 校验）。"""
        path = layout.macro_payload_path(self._root, snapshot_fingerprint)
        return _io.read_raw_bytes(path, f"macro:{snapshot_fingerprint}")

    def load_structured_payload(
        self, artifact_type: StructuredArtifactType, artifact_fingerprint: str
    ) -> dict[str, Any]:
        path = layout.structured_payload_path(self._root, artifact_type, artifact_fingerprint)
        return _io.read_json_dict(path, f"structured:{artifact_type.value}:{artifact_fingerprint}")

    def read_structured_payload_bytes(
        self, artifact_type: StructuredArtifactType, artifact_fingerprint: str
    ) -> bytes:
        """读取 structured payload 的原始 canonical bytes（供 payload_sha256 校验）。"""
        path = layout.structured_payload_path(self._root, artifact_type, artifact_fingerprint)
        return _io.read_raw_bytes(path, f"structured:{artifact_type.value}:{artifact_fingerprint}")

    def load_execution_case(self, case_id: str, case_version: int) -> LoadedEvalExecutionCase:
        """execution 侧加载：case execution fields + snapshot，不含任何 label 信息。"""
        case = self.load_case(case_id, case_version)
        snapshot = self.load_snapshot(case.source_snapshot_fingerprint)
        return LoadedEvalExecutionCase(
            case_fingerprint=compute_eval_case_fingerprint(case),
            case_id=case.case_id,
            case_version=case.case_version,
            company_id=case.company_id,
            company=case.company,
            research_question=case.research_question,
            analysis_as_of=case.analysis_as_of,
            tags=case.tags,
            snapshot=snapshot,
        )
