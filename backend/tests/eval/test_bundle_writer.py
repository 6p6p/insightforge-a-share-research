"""EvaluationBundleWriter 测试（stage 7B.1.1A）：roundtrip / replay / 拒绝覆盖 / hash。"""

from uuid import uuid4

import pytest

from app.eval.bundle.integrity import verify_bundle_integrity
from app.eval.bundle.loader import EvaluationBundleLoader
from app.eval.bundle.writer import EvaluationBundleWriter
from app.eval.contracts import FrozenSourceSnapshot, StructuredArtifactType
from app.eval.errors import EvalFingerprintError
from app.eval.fingerprints import (
    compute_eval_case_fingerprint,
    compute_human_label_fingerprint,
    compute_source_snapshot_fingerprint,
)


def test_write_load_roundtrip(built_bundle) -> None:
    root, spec = built_bundle
    loader = EvaluationBundleLoader(root)

    assert loader.load_manifest() == spec.manifest
    assert loader.load_case(spec.case.case_id, spec.case.case_version) == spec.case
    assert loader.load_snapshot(spec.snapshot_fingerprint) == spec.snapshot
    assert (
        loader.load_label(spec.label.case_id, spec.label.case_version, spec.label.label_version)
        == spec.label
    )
    assert loader.read_document_blob(spec.document_sha256) == spec.document_content
    assert loader.load_macro_payload(spec.macro_fingerprint) == spec.macro_payload
    assert (
        loader.load_structured_payload(
            StructuredArtifactType.FINANCIAL_METRIC_OBSERVATION, spec.structured_fingerprint
        )
        == spec.structured_payload
    )


def test_writer_replay_identical(built_bundle) -> None:
    root, spec = built_bundle
    writer = EvaluationBundleWriter(root)
    # 再次写入相同 frozen artifact → 应 replay（无异常，不覆盖）
    writer.write_manifest(spec.manifest)
    writer.write_case(spec.case)
    writer.write_label(spec.label)
    writer.write_snapshot(spec.snapshot)
    writer.write_document_blob(spec.document_sha256, spec.document_content)
    writer.write_macro_payload(spec.macro_ref, spec.macro_payload)
    writer.write_structured_payload(spec.structured_ref, spec.structured_payload)
    verify_bundle_integrity(root)


def test_writer_refuses_changed_case(built_bundle) -> None:
    root, spec = built_bundle
    writer = EvaluationBundleWriter(root)
    changed = spec.case.model_copy(update={"research_question": "被篡改的研究问题"})
    with pytest.raises(EvalFingerprintError):
        writer.write_case(changed)


def test_document_content_hash_mismatch(tmp_path) -> None:
    writer = EvaluationBundleWriter(tmp_path / "bundle")
    with pytest.raises(EvalFingerprintError):
        writer.write_document_blob("0" * 64, b"content whose hash is not 0" * 1)


def test_writer_rejects_case_same_path_label_change(built_bundle) -> None:
    root, spec = built_bundle
    writer = EvaluationBundleWriter(root)
    # 同 case_id/version + research input，但 human_label_fingerprint 变化。
    # case semantic fp 故意排除 label，semantic 相同也必须 reject same-path conflict。
    changed = spec.case.model_copy(update={"human_label_fingerprint": "f" * 64})
    assert compute_eval_case_fingerprint(changed) == spec.case_fingerprint
    with pytest.raises(EvalFingerprintError):
        writer.write_case(changed)


def test_writer_rejects_label_same_path_annotation_change(built_bundle) -> None:
    root, spec = built_bundle
    writer = EvaluationBundleWriter(root)
    # 同 case/version/label_version，只改 annotation。label semantic fp 故意排除
    # annotation，semantic 相同也必须 reject（不能 silent replay / overwrite）。
    changed = spec.label.model_copy(update={"annotation": "不同的备注"})
    assert compute_human_label_fingerprint(changed) == spec.label_fingerprint
    with pytest.raises(EvalFingerprintError):
        writer.write_label(changed)


def test_writer_snapshot_uuid_only_change_replays(built_bundle) -> None:
    root, spec = built_bundle
    writer = EvaluationBundleWriter(root)
    # snapshot path 由 semantic fingerprint 派生；provenance UUID-only 变化 = replay
    # （有意政策：UUID 只是 provenance pointer，不决定 frozen input 身份）。
    doc = spec.snapshot.document_sources[0]
    snapshot2 = FrozenSourceSnapshot(
        document_sources=(doc.model_copy(update={"source_record_id": uuid4()}),),
        macro_snapshots=spec.snapshot.macro_snapshots,
        structured_artifacts=spec.snapshot.structured_artifacts,
        source_providers=spec.snapshot.source_providers,
    )
    assert compute_source_snapshot_fingerprint(snapshot2) == spec.snapshot_fingerprint
    writer.write_snapshot(snapshot2)  # 不应抛异常（replay）


def test_writer_rejects_macro_payload_sha_mismatch(built_bundle) -> None:
    root, spec = built_bundle
    writer = EvaluationBundleWriter(root)
    tampered = {**spec.macro_payload, "value": 999.9}
    with pytest.raises(EvalFingerprintError):
        writer.write_macro_payload(spec.macro_ref, tampered)


def test_writer_rejects_structured_payload_sha_mismatch(built_bundle) -> None:
    root, spec = built_bundle
    writer = EvaluationBundleWriter(root)
    tampered = {**spec.structured_payload, "value": "999999999"}
    with pytest.raises(EvalFingerprintError):
        writer.write_structured_payload(spec.structured_ref, tampered)
