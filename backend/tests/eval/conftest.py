"""Synthetic bundle 共享 fixture（stage 7B.1.1A）。

构造一个完全离线的 synthetic bundle（不读真实贵州茅台 PDF / 不查 DB / 不发网络）：
- dataset: insightforge_eval_test v1
- case: test-maotai-fundamentals v1
- snapshot: 1 document + 1 macro + 1 structured
- HumanLabel: 1 FinancialFactLabel + 1 RiskTopicLabel
"""

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
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
    FrozenMacroArtifactLinkRef,
    FrozenMacroObservationRef,
    FrozenMacroRawArtifactRef,
    FrozenMacroSeriesRef,
    FrozenMacroSnapshotDetail,
    FrozenMacroSnapshotRef,
    FrozenMacroTopicRef,
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
MACRO_RAW_ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000007")
MACRO_LINK_ID = UUID("00000000-0000-0000-0000-000000000008")
MACRO_OBSERVATION_ID = UUID("00000000-0000-0000-0000-000000000009")
CASE_ID = "test-maotai-fundamentals"
DATASET_ID = "insightforge_eval_test"

# macro closure 的 raw artifact 字节（content-addressed，与 document blob 共用布局）。
MACRO_RAW_CONTENT = b'{"indicator": "SP.POP.TOTL", "rows": [{"2024": 1410000000}]}'
MACRO_RAW_SHA256 = hashlib.sha256(MACRO_RAW_CONTENT).hexdigest()


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


def _build_spec(
    *,
    doc_published_at: datetime | None = None,
    macro_fetched_at: datetime | None = None,
) -> BundleSpec:
    """构造 synthetic bundle（可参数化 source 时间以构造 no-lookahead 负例）。"""
    if doc_published_at is None:
        doc_published_at = datetime(2026, 4, 1, 12, 0, 0)
    if macro_fetched_at is None:
        macro_fetched_at = datetime(2026, 8, 1, 12, 0, 0)
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
        published_at=doc_published_at,
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
        fetched_at=macro_fetched_at,
        series=FrozenMacroSeriesRef(
            provider_key="world_bank",
            source_id="2",
            external_indicator_id="SP.POP.TOTL",
            geography_type="country",
            geography_code="CHN",
            frequency="annual",
        ),
        snapshot=FrozenMacroSnapshotDetail(
            requested_country_code="CHN",
            query_start_year=2020,
            query_end_year=2024,
            source_id_snapshot="2",
            indicator_name="Population, total",
            indicator_unit="",
            source_name="World Development Indicators",
            source_note="Total population is based on the de facto definition.",
            source_organization="World Bank",
            topics_snapshot=(
                FrozenMacroTopicRef(topic_id="19", name="Population: Structure, growth & density"),
            ),
            provider_country_id="CHN",
            iso2_code="CN",
            iso3_code="CHN",
            geography_name="China",
            region_name="East Asia & Pacific",
            income_level_name="Upper middle income",
            page=1,
            pages=1,
            per_page=50,
            provider_total=5,
            provider_last_updated="2026-01-01",
            request_count=3,
            acquisition_method="official_api",
            authority_tier_snapshot=1,
            critical_claim_eligible_snapshot=True,
            provider_capabilities_snapshot=("macro_data", "document_download"),
            fingerprint_version=1,
            normalization_version="world_bank_v1",
            status="available",
        ),
        observations=(
            FrozenMacroObservationRef(
                observation_id=MACRO_OBSERVATION_ID,
                period="2024",
                normalized_period_start=date(2024, 1, 1),
                value_numeric=Decimal("1410000000"),
                is_missing=False,
                decimal_scale=0,
                observation_status="",
                period_semantics="provider_year_label",
                frequency="annual",
            ),
        ),
        artifact_links=(
            FrozenMacroArtifactLinkRef(
                snapshot_artifact_id=MACRO_LINK_ID,
                artifact_id=MACRO_RAW_ARTIFACT_ID,
                role="observations_page",
                page=1,
                response_status=200,
                final_hostname="api.worldbank.org",
                content_type="application/json",
                fetched_at=macro_fetched_at,
            ),
        ),
        raw_artifacts=(
            FrozenMacroRawArtifactRef(
                artifact_id=MACRO_RAW_ARTIFACT_ID,
                content_sha256=MACRO_RAW_SHA256,
                media_type="application/json",
                byte_size=len(MACRO_RAW_CONTENT),
            ),
        ),
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


def build_bundle(
    root: str | Path,
    *,
    doc_published_at: datetime | None = None,
    macro_fetched_at: datetime | None = None,
) -> BundleSpec:
    """把完整 synthetic bundle 写入 root（幂等，可重复调用）。

    `doc_published_at` / `macro_fetched_at` 可参数化：默认是合法过去时间；传未来
    时间可构造自洽但违反 no-lookahead 的负例 bundle。
    """
    spec = _build_spec(
        doc_published_at=doc_published_at,
        macro_fetched_at=macro_fetched_at,
    )
    writer = EvaluationBundleWriter(root)
    writer.write_document_blob(spec.document_sha256, spec.document_content)
    writer.write_document_blob(MACRO_RAW_SHA256, MACRO_RAW_CONTENT)
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
