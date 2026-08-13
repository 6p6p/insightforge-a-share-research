"""Eval 数据契约测试（stage 7B.1.0）。"""

from datetime import datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.eval.contracts import (
    ClaimSupportLabel,
    ClaimSupportStatus,
    EvalCase,
    EvalComponentVersion,
    EvalDatasetCaseRef,
    EvalDatasetManifest,
    EvalExecutionConfig,
    EvalExecutionSpec,
    FinancialFactLabel,
    FrozenCompanyIdentity,
    FrozenDocumentSourceRef,
    FrozenMacroSnapshotRef,
    FrozenModelConfig,
    FrozenSourceSnapshot,
    FrozenStructuredArtifactRef,
    HumanLabel,
    MacroCausalLabel,
    RiskTopicLabel,
    StructuredArtifactType,
)
from app.eval.errors import EvalContractError, EvalError, EvalFingerprintError, EvalVariantError
from app.eval.variants import EvalVariantId


def _sha(tag: str = "a") -> str:
    return tag * 64


def _doc_ref(tag: str = "a", **overrides) -> FrozenDocumentSourceRef:
    kwargs = dict(
        source_record_id=uuid4(),
        raw_artifact_id=uuid4(),
        content_sha256=_sha(tag),
        provider_key="cninfo",
        document_type="annual_report",
        media_type="application/pdf",
        title="测试文档",
        source_url="https://example.com/doc",
        acquired_at=datetime(2026, 8, 1, 12, 0, 0),
        authority_tier_snapshot=3,
        critical_claim_eligible_snapshot=False,
    )
    kwargs.update(overrides)
    return FrozenDocumentSourceRef(**kwargs)


def _company(**overrides) -> FrozenCompanyIdentity:
    kwargs = dict(
        security_code="000001",
        official_name="Acme 股份",
        exchange="SSE",
        board="sse_main",
    )
    kwargs.update(overrides)
    return FrozenCompanyIdentity(**kwargs)


def _case(**overrides) -> EvalCase:
    kwargs = dict(
        case_id="acme-2024-fundamental",
        case_version=1,
        company_id=uuid4(),
        company=_company(),
        research_question="Acme 2024 年基本面是否支撑当前估值？",
        analysis_as_of=datetime(2026, 8, 1, 12, 0, 0),
        source_snapshot_fingerprint=_sha("b"),
    )
    kwargs.update(overrides)
    return EvalCase(**kwargs)


def test_contracts_frozen() -> None:
    case = _case()
    with pytest.raises(ValidationError):
        case.case_id = "mutated"


def test_invalid_hex_char_rejected() -> None:
    with pytest.raises(ValidationError):
        _doc_ref("a", content_sha256="g" * 64)


def test_wrong_length_hex_rejected() -> None:
    with pytest.raises(ValidationError):
        _doc_ref("a", content_sha256="ab")


def test_duplicate_source_identity_rejected() -> None:
    ref1 = _doc_ref("a")
    ref2 = _doc_ref("a")
    with pytest.raises(ValidationError):
        FrozenSourceSnapshot(document_sources=(ref1, ref2))


def test_duplicate_dataset_case_rejected() -> None:
    ref = EvalDatasetCaseRef(case_id="acme-2024", case_version=1, case_fingerprint=_sha("a"))
    with pytest.raises(ValidationError):
        EvalDatasetManifest(dataset_id="a_share_eval_v1", dataset_version=1, cases=(ref, ref))


def test_case_id_rejects_uuid_only() -> None:
    with pytest.raises(ValidationError):
        _case(case_id=str(uuid4()))


def test_case_excludes_runtime_fields() -> None:
    forbidden = {
        "workflow_run_id",
        "orchestration_id",
        "created_at",
        "status",
        "execution_status",
    }
    assert forbidden.isdisjoint(set(EvalCase.model_fields))


def test_snapshot_covers_three_categories() -> None:
    snapshot = FrozenSourceSnapshot(
        document_sources=(_doc_ref("a"),),
        macro_snapshots=(
            FrozenMacroSnapshotRef(
                snapshot_id=uuid4(),
                series_id=uuid4(),
                snapshot_fingerprint=_sha("c"),
                payload_sha256=_sha("e"),
                fetched_at=datetime(2026, 8, 1, 12, 0, 0),
            ),
        ),
        structured_artifacts=(
            FrozenStructuredArtifactRef(
                artifact_type=StructuredArtifactType.FINANCIAL_METRIC_OBSERVATION,
                artifact_id=uuid4(),
                artifact_fingerprint=_sha("d"),
                payload_sha256=_sha("f"),
            ),
        ),
    )
    assert len(snapshot.document_sources) == 1
    assert len(snapshot.macro_snapshots) == 1
    assert len(snapshot.structured_artifacts) == 1


def test_structured_artifact_type_enum() -> None:
    assert (
        StructuredArtifactType.FINANCIAL_METRIC_OBSERVATION.value == "financial_metric_observation"
    )
    assert (
        StructuredArtifactType.RELATIVE_VALUATION_OBSERVATION.value
        == "relative_valuation_observation"
    )
    assert (
        StructuredArtifactType.RELATIVE_VALUATION_COMPARISON.value
        == "relative_valuation_comparison"
    )
    with pytest.raises(ValidationError):
        FrozenStructuredArtifactRef(
            artifact_type="unknown",
            artifact_id=uuid4(),
            artifact_fingerprint=_sha("d"),
            payload_sha256=_sha("f"),
        )


def test_human_label_all_four_typed_collections() -> None:
    label = HumanLabel(
        case_id="acme-2024",
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
        claim_support_labels=(
            ClaimSupportLabel(
                claim_label_id="c1",
                expected_support_status=ClaimSupportStatus.SUPPORTED,
            ),
        ),
        macro_causal_labels=(
            MacroCausalLabel(
                driver_code="gdp_growth",
                company_exposure_expected=True,
                causal_claim_allowed=True,
            ),
        ),
    )
    assert label.financial_facts[0].label_type == "financial_fact"
    assert label.risk_topics[0].label_type == "risk_topic"
    assert label.claim_support_labels[0].label_type == "claim_support"
    assert label.macro_causal_labels[0].label_type == "macro_causal"


def test_human_label_wrong_typed_item_rejected() -> None:
    with pytest.raises(ValidationError):
        HumanLabel(
            case_id="acme-2024",
            case_version=1,
            label_version=1,
            financial_facts=(RiskTopicLabel(risk_code="R1", required=True),),
        )


def test_label_discriminator_literal() -> None:
    with pytest.raises(ValidationError):
        FinancialFactLabel(
            label_type="risk_topic",
            metric_code="net_profit",
            period="FY2024",
            unit="cny_yuan",
            expected_value=Decimal("1"),
        )


def test_tolerance_nonnegative() -> None:
    with pytest.raises(ValidationError):
        FinancialFactLabel(
            metric_code="net_profit",
            period="FY2024",
            unit="cny_yuan",
            expected_value=Decimal("1"),
            absolute_tolerance=Decimal("-1"),
        )


def test_annotation_is_separate_non_ground_truth_field() -> None:
    label = HumanLabel(
        case_id="acme-2024",
        case_version=1,
        label_version=1,
        annotation="human note",
    )
    assert label.annotation == "human note"
    assert label.financial_facts == ()
    assert label.risk_topics == ()
    assert label.claim_support_labels == ()
    assert label.macro_causal_labels == ()


def test_execution_spec_excludes_label_fields() -> None:
    spec = EvalExecutionSpec(
        case_fingerprint=_sha("a"),
        source_snapshot_fingerprint=_sha("b"),
        execution_config_fingerprint=_sha("c"),
        variant_id=EvalVariantId.INSIGHTFORGE_FULL,
    )
    fields = set(EvalExecutionSpec.model_fields)
    assert "human_label_fingerprint" not in fields
    assert "metric_registry_version" not in fields
    assert "judge_config_fingerprint" not in fields
    assert spec.variant_id == EvalVariantId.INSIGHTFORGE_FULL


def _macro_ref(fp: str) -> FrozenMacroSnapshotRef:
    return FrozenMacroSnapshotRef(
        snapshot_id=uuid4(),
        series_id=uuid4(),
        snapshot_fingerprint=fp,
        payload_sha256=_sha("e"),
        fetched_at=datetime(2026, 8, 1, 12, 0, 0),
    )


def test_macro_duplicate_fingerprint_rejected() -> None:
    with pytest.raises(ValidationError):
        FrozenSourceSnapshot(macro_snapshots=(_macro_ref(_sha("a")), _macro_ref(_sha("a"))))


def test_structured_duplicate_fingerprint_rejected() -> None:
    fp = _sha("b")
    ref1 = FrozenStructuredArtifactRef(
        artifact_type=StructuredArtifactType.FINANCIAL_METRIC_OBSERVATION,
        artifact_id=uuid4(),
        artifact_fingerprint=fp,
        payload_sha256=_sha("f"),
    )
    ref2 = FrozenStructuredArtifactRef(
        artifact_type=StructuredArtifactType.FINANCIAL_METRIC_OBSERVATION,
        artifact_id=uuid4(),
        artifact_fingerprint=fp,
        payload_sha256=_sha("f"),
    )
    with pytest.raises(ValidationError):
        FrozenSourceSnapshot(structured_artifacts=(ref1, ref2))


def test_document_duplicate_hash_different_uuid_provider_rejected() -> None:
    ref1 = _doc_ref("d")
    ref2 = _doc_ref("d", provider_key="sse")
    with pytest.raises(ValidationError):
        FrozenSourceSnapshot(document_sources=(ref1, ref2))


def test_distinct_semantic_identity_allowed() -> None:
    snapshot = FrozenSourceSnapshot(
        document_sources=(_doc_ref("a"), _doc_ref("b")),
        macro_snapshots=(_macro_ref(_sha("c")), _macro_ref(_sha("d"))),
    )
    assert len(snapshot.document_sources) == 2
    assert len(snapshot.macro_snapshots) == 2


def _execution_config(**overrides) -> EvalExecutionConfig:
    kwargs = dict(
        variant_id=EvalVariantId.INSIGHTFORGE_FULL,
        model=FrozenModelConfig(
            provider="deepseek",
            model_id="deepseek-v4-flash",
            thinking_enabled=False,
            temperature=Decimal("0"),
            structured_output=True,
        ),
        variant_version="1",
        prompt_version="1",
        retrieval_version="1",
        pipeline_version="1",
    )
    kwargs.update(overrides)
    return EvalExecutionConfig(**kwargs)


def test_component_version_duplicate_name_rejected() -> None:
    with pytest.raises(ValidationError):
        _execution_config(
            component_versions=(
                EvalComponentVersion(component_name="audit", component_version="v1"),
                EvalComponentVersion(component_name="audit", component_version="v2"),
            )
        )


def test_component_version_canonical_sort() -> None:
    config = _execution_config(
        component_versions=(
            EvalComponentVersion(component_name="evidence_extractor", component_version="v2"),
            EvalComponentVersion(component_name="audit", component_version="v1"),
        )
    )
    assert [c.component_name for c in config.component_versions] == [
        "audit",
        "evidence_extractor",
    ]


def test_error_codes_stable() -> None:
    assert EvalError.code == "eval_error"
    assert EvalContractError.code == "eval_contract_error"
    assert EvalFingerprintError.code == "eval_fingerprint_error"
    assert EvalVariantError.code == "eval_variant_error"
