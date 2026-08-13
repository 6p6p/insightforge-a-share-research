"""Bundle referential integrity 测试（stage 7B.1.1A）：tamper / 缺失 / envelope / portability。"""

import json
import shutil

import pytest

from app.eval.bundle import layout
from app.eval.bundle.integrity import verify_bundle_integrity
from app.eval.canonical import canonical_json_bytes
from app.eval.contracts import StructuredArtifactType
from app.eval.errors import EvalContractError


def test_verify_bundle_integrity_ok(built_bundle) -> None:
    root, spec = built_bundle
    verified = verify_bundle_integrity(root)
    assert verified.dataset_id == "insightforge_eval_test"
    assert verified.dataset_version == 1
    assert verified.dataset_fingerprint == spec.dataset_fingerprint
    assert verified.cases == spec.manifest.cases


def test_tamper_document_blob_verify_fails(built_bundle) -> None:
    root, spec = built_bundle
    layout.document_blob_path(root, spec.document_sha256).write_bytes(b"tampered content")
    with pytest.raises(EvalContractError):
        verify_bundle_integrity(root)


def test_tamper_case_json_verify_fails(built_bundle) -> None:
    root, spec = built_bundle
    case_path = layout.case_path(root, spec.case.case_id, spec.case.case_version)
    modified = spec.case.model_copy(update={"research_question": "tampered question"})
    case_path.write_bytes(canonical_json_bytes(modified.model_dump(mode="json")) + b"\n")
    with pytest.raises(EvalContractError):
        verify_bundle_integrity(root)


def test_manifest_case_fingerprint_mismatch_verify_fails(built_bundle) -> None:
    root, spec = built_bundle
    manifest_path = layout.manifest_path(root)
    data = json.loads(manifest_path.read_bytes().decode("utf-8"))
    data["cases"][0]["case_fingerprint"] = "3" * 64
    manifest_path.write_bytes(canonical_json_bytes(data) + b"\n")
    with pytest.raises(EvalContractError):
        verify_bundle_integrity(root)


def test_missing_snapshot_verify_fails(built_bundle) -> None:
    root, spec = built_bundle
    layout.snapshot_path(root, spec.snapshot_fingerprint).unlink()
    with pytest.raises(EvalContractError):
        verify_bundle_integrity(root)


def test_missing_label_verify_fails(built_bundle) -> None:
    root, spec = built_bundle
    layout.label_path(
        root, spec.label.case_id, spec.label.case_version, spec.label.label_version
    ).unlink()
    with pytest.raises(EvalContractError):
        verify_bundle_integrity(root)


def test_wrong_label_case_id_verify_fails(built_bundle) -> None:
    root, spec = built_bundle
    label_path = layout.label_path(
        root, spec.label.case_id, spec.label.case_version, spec.label.label_version
    )
    modified = spec.label.model_copy(update={"case_id": "wrong-case"})
    label_path.write_bytes(canonical_json_bytes(modified.model_dump(mode="json")) + b"\n")
    with pytest.raises(EvalContractError):
        verify_bundle_integrity(root)


def test_missing_macro_payload_verify_fails(built_bundle) -> None:
    root, spec = built_bundle
    layout.macro_payload_path(root, spec.macro_fingerprint).unlink()
    with pytest.raises(EvalContractError):
        verify_bundle_integrity(root)


def test_macro_envelope_fp_mismatch_verify_fails(built_bundle) -> None:
    root, spec = built_bundle
    path = layout.macro_payload_path(root, spec.macro_fingerprint)
    tampered = {**spec.macro_payload, "snapshot_fingerprint": "4" * 64}
    path.write_bytes(canonical_json_bytes(tampered))
    with pytest.raises(EvalContractError):
        verify_bundle_integrity(root)


def test_tamper_macro_payload_value_verify_fails(built_bundle) -> None:
    root, spec = built_bundle
    path = layout.macro_payload_path(root, spec.macro_fingerprint)
    # 只改 value，snapshot_fingerprint envelope 不变 → 靠 payload_sha256 捕获。
    tampered = {**spec.macro_payload, "value": 999.9}
    assert tampered["snapshot_fingerprint"] == spec.macro_fingerprint
    path.write_bytes(canonical_json_bytes(tampered))
    with pytest.raises(EvalContractError):
        verify_bundle_integrity(root)


def test_missing_structured_payload_verify_fails(built_bundle) -> None:
    root, spec = built_bundle
    layout.structured_payload_path(
        root, StructuredArtifactType.FINANCIAL_METRIC_OBSERVATION, spec.structured_fingerprint
    ).unlink()
    with pytest.raises(EvalContractError):
        verify_bundle_integrity(root)


def test_structured_envelope_mismatch_verify_fails(built_bundle) -> None:
    root, spec = built_bundle
    path = layout.structured_payload_path(
        root, StructuredArtifactType.FINANCIAL_METRIC_OBSERVATION, spec.structured_fingerprint
    )
    tampered = {**spec.structured_payload, "artifact_fingerprint": "5" * 64}
    path.write_bytes(canonical_json_bytes(tampered))
    with pytest.raises(EvalContractError):
        verify_bundle_integrity(root)


def test_tamper_structured_payload_field_verify_fails(built_bundle) -> None:
    root, spec = built_bundle
    path = layout.structured_payload_path(
        root, StructuredArtifactType.FINANCIAL_METRIC_OBSERVATION, spec.structured_fingerprint
    )
    # 只改数据 field，artifact_fingerprint envelope 不变 → 靠 payload_sha256 捕获。
    tampered = {**spec.structured_payload, "value": "999999999"}
    assert tampered["artifact_fingerprint"] == spec.structured_fingerprint
    path.write_bytes(canonical_json_bytes(tampered))
    with pytest.raises(EvalContractError):
        verify_bundle_integrity(root)


def test_dataset_fingerprint_stable_across_root_change(built_bundle) -> None:
    root, spec = built_bundle
    root2 = root.parent / "bundle_copy"
    shutil.copytree(root, root2)
    verified1 = verify_bundle_integrity(root)
    verified2 = verify_bundle_integrity(root2)
    assert verified1.dataset_fingerprint == verified2.dataset_fingerprint
    assert verified1.dataset_fingerprint == spec.dataset_fingerprint
