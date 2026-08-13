"""EvaluationBundleWriter 测试（stage 7B.1.1A）：roundtrip / replay / 拒绝覆盖 / hash。"""

import pytest

from app.eval.bundle.integrity import verify_bundle_integrity
from app.eval.bundle.loader import EvaluationBundleLoader
from app.eval.bundle.writer import EvaluationBundleWriter
from app.eval.contracts import StructuredArtifactType
from app.eval.errors import EvalFingerprintError


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
    writer.write_macro_payload(spec.macro_fingerprint, spec.macro_payload)
    writer.write_structured_payload(
        StructuredArtifactType.FINANCIAL_METRIC_OBSERVATION,
        spec.structured_fingerprint,
        spec.structured_payload,
    )
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
