"""Synthetic bundle 共享 fixture（stage 7B.1.1A）。

构造一个完全离线的 synthetic bundle（不读真实贵州茅台 PDF / 不查 DB / 不发网络）：
- dataset: insightforge_eval_test v1
- case: test-maotai-fundamentals v1
- snapshot: 1 document + 1 macro + 1 structured
- HumanLabel: 1 FinancialFactLabel + 1 RiskTopicLabel
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from app.eval.bundle.writer import EvaluationBundleWriter
from app.eval.canonical import canonical_json_bytes
from app.eval.contracts import (
    EvalCase,
    EvalDatasetCaseRef,
    EvalDatasetManifest,
    FinancialFactLabel,
    FrozenCompanyIdentity,
    FrozenDocumentSourceRef,
    FrozenMacroSnapshotRef,
    FrozenSourceProviderRef,
    FrozenSourceSnapshot,
    FrozenStructuredArtifactRef,
    HumanLabel,
    RiskTopicLabel,
    StructuredArtifactType,
)
from app.eval.fingerprints import (
    compute_dataset_fingerprint,
    compute_eval_case_fingerprint,
    compute_human_label_fingerprint,
    compute_source_snapshot_fingerprint,
)

# ---- 固定 synthetic identity（deterministic，跨 root 可复现）----

DOC_CONTENT = b"InsightForge synthetic annual report bytes (maotai test, not real)"
DOC_SHA256 = hashlib.sha256(DOC_CONTENT).hexdigest()
MACRO_FP = "1" * 64
STRUCTURED_FP = "2" * 64
COMPANY_ID = UUID("00000000-0000-0000-0000-000000000001")
SOURCE_RECORD_ID = UUID("00000000-0000-0000-0000-000000000002")
RAW_ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000003")
MACRO_SNAPSHOT_ID = UUID("00000000-0000-0000-0000-000000000004")
MACRO_SERIES_ID = UUID("00000000-0000-0000-0000-000000000005")
STRUCTURED_ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000006")
CASE_ID = "test-maotai-fundamentals"
DATASET_ID = "insightforge_eval_test"


@dataclass(frozen=True)
class BundleSpec:
    manifest: EvalDatasetManifest
    case: EvalCase
    label: HumanLabel
    snapshot: FrozenSourceSnapshot
    macro_ref: FrozenMacroSnapshotRef
    structured_ref: FrozenStructuredArtifactRef
    document_content: bytes
    document_sha256: str
    macro_fingerprint: str
    macro_payload: dict
    structured_fingerprint: str
    structured_payload: dict
    snapshot_fingerprint: str
    label_fingerprint: str
    case_fingerprint: str
    dataset_fingerprint: str


def _build_spec() -> BundleSpec:
    macro_payload = {"snapshot_fingerprint": MACRO_FP, "series": "gdp_growth", "value": 5.2}
    structured_payload = {
        "artifact_type": StructuredArtifactType.FINANCIAL_METRIC_OBSERVATION.value,
        "artifact_fingerprint": STRUCTURED_FP,
        "metric_code": "net_profit",
        "value": "100000000",
    }
    macro_payload_sha = hashlib.sha256(canonical_json_bytes(macro_payload)).hexdigest()
    structured_payload_sha = hashlib.sha256(canonical_json_bytes(structured_payload)).hexdigest()

    doc = FrozenDocumentSourceRef(
        source_record_id=SOURCE_RECORD_ID,
        raw_artifact_id=RAW_ARTIFACT_ID,
        content_sha256=DOC_SHA256,
        provider_key="cninfo",
        document_type="annual_report",
        media_type="application/pdf",
        title="贵州茅台 2024 年年度报告",
        source_url="https://www.cninfo.com.cn/maotai/annual_report.pdf",
        acquired_at=datetime(2026, 8, 1, 12, 0, 0),
        authority_tier_snapshot=1,
        critical_claim_eligible_snapshot=True,
        published_at=datetime(2026, 4, 1, 12, 0, 0),
    )
    provider = FrozenSourceProviderRef(
        provider_key="cninfo",
        display_name="巨潮资讯",
        enabled=True,
        capabilities=("annual_report",),
    )
    macro_ref = FrozenMacroSnapshotRef(
        snapshot_id=MACRO_SNAPSHOT_ID,
        series_id=MACRO_SERIES_ID,
        snapshot_fingerprint=MACRO_FP,
        payload_sha256=macro_payload_sha,
        fetched_at=datetime(2026, 8, 1, 12, 0, 0),
    )
    structured_ref = FrozenStructuredArtifactRef(
        artifact_type=StructuredArtifactType.FINANCIAL_METRIC_OBSERVATION,
        artifact_id=STRUCTURED_ARTIFACT_ID,
        artifact_fingerprint=STRUCTURED_FP,
        payload_sha256=structured_payload_sha,
    )
    snapshot = FrozenSourceSnapshot(
        document_sources=(doc,),
        macro_snapshots=(macro_ref,),
        structured_artifacts=(structured_ref,),
        source_providers=(provider,),
    )
    snapshot_fp = compute_source_snapshot_fingerprint(snapshot)

    label = HumanLabel(
        case_id=CASE_ID,
        case_version=1,
        label_version=1,
        financial_facts=(
            FinancialFactLabel(
                metric_code="net_profit",
                period="FY2024",
                unit="cny_yuan",
                expected_value=Decimal("100000000"),
            ),
        ),
        risk_topics=(RiskTopicLabel(risk_code="R1", required=True),),
    )
    label_fp = compute_human_label_fingerprint(label)

    case = EvalCase(
        case_id=CASE_ID,
        case_version=1,
        company_id=COMPANY_ID,
        company=FrozenCompanyIdentity(
            security_code="600519",
            official_name="贵州茅台酒股份有限公司",
            short_name="贵州茅台",
            exchange="SSE",
            board="sse_main",
            aliases=("贵州茅台",),
        ),
        research_question="贵州茅台 2024 年基本面是否支撑当前估值？",
        analysis_as_of=datetime(2026, 8, 1, 12, 0, 0),
        source_snapshot_fingerprint=snapshot_fp,
        human_label_fingerprint=label_fp,
    )
    case_fp = compute_eval_case_fingerprint(case)

    manifest = EvalDatasetManifest(
        dataset_id=DATASET_ID,
        dataset_version=1,
        cases=(EvalDatasetCaseRef(case_id=CASE_ID, case_version=1, case_fingerprint=case_fp),),
    )
    dataset_fp = compute_dataset_fingerprint(manifest)

    return BundleSpec(
        manifest=manifest,
        case=case,
        label=label,
        snapshot=snapshot,
        macro_ref=macro_ref,
        structured_ref=structured_ref,
        document_content=DOC_CONTENT,
        document_sha256=DOC_SHA256,
        macro_fingerprint=MACRO_FP,
        macro_payload=macro_payload,
        structured_fingerprint=STRUCTURED_FP,
        structured_payload=structured_payload,
        snapshot_fingerprint=snapshot_fp,
        label_fingerprint=label_fp,
        case_fingerprint=case_fp,
        dataset_fingerprint=dataset_fp,
    )


def build_bundle(root: str | Path) -> BundleSpec:
    """把完整 synthetic bundle 写入 root（幂等，可重复调用）。"""
    spec = _build_spec()
    writer = EvaluationBundleWriter(root)
    writer.write_document_blob(spec.document_sha256, spec.document_content)
    writer.write_macro_payload(spec.macro_ref, spec.macro_payload)
    writer.write_structured_payload(spec.structured_ref, spec.structured_payload)
    writer.write_snapshot(spec.snapshot)
    writer.write_label(spec.label)
    writer.write_case(spec.case)
    writer.write_manifest(spec.manifest)
    return spec


@pytest.fixture
def built_bundle(tmp_path: Path) -> tuple[Path, BundleSpec]:
    root = tmp_path / "bundle"
    return root, build_bundle(root)
