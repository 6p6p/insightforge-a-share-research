"""Bundle referential integrity verification（stage 7B.1.1A）。

验证 bundle 内部引用闭合：manifest → case → snapshot → document / macro / structured
payload。document blob / macro payload / structured payload 按字节 SHA 校验
（content_sha256 / payload_sha256）；不在此处调用真实 domain verifier（那属于 7B.1.1B
从 PG materialize 时的 upstream fingerprint 证明）。
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.eval.bundle.loader import EvaluationBundleLoader
from app.eval.contracts import EvalDatasetCaseRef
from app.eval.errors import EvalContractError
from app.eval.fingerprints import (
    compute_dataset_fingerprint,
    compute_eval_case_fingerprint,
    compute_human_label_fingerprint,
    compute_source_snapshot_fingerprint,
)


@dataclass(frozen=True)
class VerifiedEvaluationBundle:
    """verify_bundle_integrity 成功后的 read model。"""

    dataset_id: str
    dataset_version: int
    dataset_fingerprint: str
    cases: tuple[EvalDatasetCaseRef, ...]


def verify_bundle_integrity(root: str | Path) -> VerifiedEvaluationBundle:
    """验证 bundle 引用闭合；失败抛 `EvalContractError`（成功返回 read model）。"""
    loader = EvaluationBundleLoader(root)
    manifest = loader.load_manifest()

    # 1. dataset fingerprint 可重新计算（manifest 不保存自身 fingerprint，避免递归）。
    dataset_fingerprint = compute_dataset_fingerprint(manifest)

    for ref in manifest.cases:
        # 2. 每个 DatasetCaseRef 对应 case 实际存在。
        case = loader.load_case(ref.case_id, ref.case_version)
        # 3. case_id / version 与 ref 匹配。
        if case.case_id != ref.case_id or case.case_version != ref.case_version:
            raise EvalContractError(f"case:{ref.case_id} v{ref.case_version} 身份不匹配")
        # 4. recompute case fingerprint == manifest case_fingerprint。
        if compute_eval_case_fingerprint(case) != ref.case_fingerprint:
            raise EvalContractError(f"case:{ref.case_id} v{ref.case_version} fingerprint 不匹配")

        # 5. snapshot 实际存在。
        snapshot = loader.load_snapshot(case.source_snapshot_fingerprint)
        # 6. recompute snapshot fingerprint == case 引用。
        if compute_source_snapshot_fingerprint(snapshot) != case.source_snapshot_fingerprint:
            raise EvalContractError(f"snapshot 与 case:{case.case_id} 引用不匹配")

        # 7-9. label（若非 None）→ 唯一 label + case_id/version 匹配 + fingerprint 匹配。
        if case.human_label_fingerprint is not None:
            label = loader.load_label_by_fingerprint(
                case.case_id, case.case_version, case.human_label_fingerprint
            )
            if label.case_id != case.case_id or label.case_version != case.case_version:
                raise EvalContractError(f"label 与 case:{case.case_id} 身份不匹配")
            if compute_human_label_fingerprint(label) != case.human_label_fingerprint:
                raise EvalContractError(f"label 与 case:{case.case_id} fingerprint 不匹配")

        # 10. document blob content-address 闭合。
        for doc in snapshot.document_sources:
            blob = loader.read_document_blob(doc.content_sha256)
            if hashlib.sha256(blob).hexdigest() != doc.content_sha256:
                raise EvalContractError("document blob content_sha256 不匹配")

        # 11. macro payload：file bytes SHA == payload_sha256 + envelope 闭合。
        for macro in snapshot.macro_snapshots:
            raw = loader.read_macro_payload_bytes(macro.snapshot_fingerprint)
            if hashlib.sha256(raw).hexdigest() != macro.payload_sha256:
                raise EvalContractError("macro payload 字节 SHA 与 payload_sha256 不匹配")
            payload = loader.load_macro_payload(macro.snapshot_fingerprint)
            if payload.get("snapshot_fingerprint") != macro.snapshot_fingerprint:
                raise EvalContractError("macro payload envelope snapshot_fingerprint 不匹配")

        # 11b. macro raw artifact blob：bytes SHA == content_sha256 + byte_size 精确
        #      （media_type 契约已由 FrozenMacroRawArtifactRef._v_media_type 在解析
        #      时强制为 application/json；role 与 artifact_links 的 1:1 由
        #      FrozenMacroSnapshotRef 跨字段 validator 保证，这里不再重复）。
        for macro in snapshot.macro_snapshots:
            for raw_ref in macro.raw_artifacts:
                blob = loader.read_document_blob(raw_ref.content_sha256)
                if hashlib.sha256(blob).hexdigest() != raw_ref.content_sha256:
                    raise EvalContractError("macro raw artifact blob content_sha256 不匹配")
                if len(blob) != raw_ref.byte_size:
                    raise EvalContractError("macro raw artifact blob byte_size 不匹配")

        # 12. structured payload：file bytes SHA == payload_sha256 + envelope 闭合。
        for art in snapshot.structured_artifacts:
            raw = loader.read_structured_payload_bytes(art.artifact_type, art.artifact_fingerprint)
            if hashlib.sha256(raw).hexdigest() != art.payload_sha256:
                raise EvalContractError("structured payload 字节 SHA 与 payload_sha256 不匹配")
            payload = loader.load_structured_payload(art.artifact_type, art.artifact_fingerprint)
            if payload.get("artifact_type") != art.artifact_type.value:
                raise EvalContractError("structured payload envelope artifact_type 不匹配")
            if payload.get("artifact_fingerprint") != art.artifact_fingerprint:
                raise EvalContractError("structured payload envelope artifact_fingerprint 不匹配")

    return VerifiedEvaluationBundle(
        dataset_id=manifest.dataset_id,
        dataset_version=manifest.dataset_version,
        dataset_fingerprint=dataset_fingerprint,
        cases=manifest.cases,
    )
