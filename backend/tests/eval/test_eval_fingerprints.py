"""Eval fingerprint 测试（stage 7B.1.0）。"""

from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from app.eval.canonical import canonical_json_str
from app.eval.contracts import (
    EvalCase,
    EvalComponentVersion,
    EvalDatasetCaseRef,
    EvalDatasetManifest,
    EvalExecutionConfig,
    EvalExecutionSpec,
    EvalScoringSpec,
    EvalVariantOutput,
    FinancialFactLabel,
    FrozenDocumentSourceRef,
    FrozenMacroSnapshotRef,
    FrozenModelConfig,
    FrozenSourceSnapshot,
    HumanLabel,
)
from app.eval.fingerprints import (
    compute_dataset_fingerprint,
    compute_eval_case_fingerprint,
    compute_execution_config_fingerprint,
    compute_execution_spec_fingerprint,
    compute_human_label_fingerprint,
    compute_scoring_spec_fingerprint,
    compute_source_snapshot_fingerprint,
    compute_variant_output_fingerprint,
)
from app.eval.variants import EvalVariantId


def _sha(tag: str = "a") -> str:
    return tag * 64


def _doc_ref(tag: str = "a") -> FrozenDocumentSourceRef:
    return FrozenDocumentSourceRef(
        source_record_id=uuid4(),
        raw_artifact_id=uuid4(),
        content_sha256=_sha(tag),
        provider_key="cninfo",
        document_type="annual_report",
        media_type="application/pdf",
    )


def _case(company_id=None, label_fp=None) -> EvalCase:
    return EvalCase(
        case_id="acme-2024-fundamental",
        case_version=1,
        company_id=company_id if company_id is not None else uuid4(),
        security_code="000001",
        research_question="Acme 2024 年基本面是否支撑当前估值？",
        analysis_as_of=datetime(2026, 8, 1, 12, 0, 0),
        source_snapshot_fingerprint=_sha("b"),
        human_label_fingerprint=label_fp,
    )


def _execution_config() -> EvalExecutionConfig:
    return EvalExecutionConfig(
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


def test_dict_key_order_independent() -> None:
    assert canonical_json_str({"a": 1, "b": 2}) == canonical_json_str({"b": 2, "a": 1})


def test_dataset_fingerprint_deterministic() -> None:
    manifest = EvalDatasetManifest(
        dataset_id="a_share_eval_v1",
        dataset_version=1,
        cases=(
            EvalDatasetCaseRef(case_id="acme-2024", case_version=1, case_fingerprint=_sha("a")),
            EvalDatasetCaseRef(case_id="beta-2024", case_version=1, case_fingerprint=_sha("b")),
        ),
    )
    assert compute_dataset_fingerprint(manifest) == compute_dataset_fingerprint(manifest)


def test_dataset_fingerprint_case_order_independent() -> None:
    a = EvalDatasetCaseRef(case_id="acme-2024", case_version=1, case_fingerprint=_sha("a"))
    b = EvalDatasetCaseRef(case_id="beta-2024", case_version=1, case_fingerprint=_sha("b"))
    m1 = EvalDatasetManifest(dataset_id="d", dataset_version=1, cases=(a, b))
    m2 = EvalDatasetManifest(dataset_id="d", dataset_version=1, cases=(b, a))
    assert compute_dataset_fingerprint(m1) == compute_dataset_fingerprint(m2)


def test_snapshot_semantic_change_changes_fp() -> None:
    s1 = FrozenSourceSnapshot(document_sources=(_doc_ref("a"),))
    s2 = FrozenSourceSnapshot(document_sources=(_doc_ref("b"),))
    assert compute_source_snapshot_fingerprint(s1) != compute_source_snapshot_fingerprint(s2)


def test_snapshot_uuid_only_change_keeps_fp() -> None:
    s1 = FrozenSourceSnapshot(document_sources=(_doc_ref("a"),))
    # 同 content_sha256 + metadata，不同 DB UUID → semantic identity 不变
    s2 = FrozenSourceSnapshot(
        document_sources=(
            FrozenDocumentSourceRef(
                source_record_id=uuid4(),
                raw_artifact_id=uuid4(),
                content_sha256=_sha("a"),
                provider_key="cninfo",
                document_type="annual_report",
                media_type="application/pdf",
            ),
        ),
    )
    assert compute_source_snapshot_fingerprint(s1) == compute_source_snapshot_fingerprint(s2)


def test_snapshot_reorder_keeps_fp() -> None:
    s1 = FrozenSourceSnapshot(document_sources=(_doc_ref("a"), _doc_ref("b")))
    s2 = FrozenSourceSnapshot(document_sources=(_doc_ref("b"), _doc_ref("a")))
    assert compute_source_snapshot_fingerprint(s1) == compute_source_snapshot_fingerprint(s2)


def test_snapshot_fingerprint_includes_macro_payload_sha() -> None:
    def make(payload_sha: str) -> FrozenSourceSnapshot:
        return FrozenSourceSnapshot(
            macro_snapshots=(
                FrozenMacroSnapshotRef(
                    snapshot_id=uuid4(),
                    series_id=uuid4(),
                    snapshot_fingerprint=_sha("c"),
                    payload_sha256=payload_sha,
                    fetched_at=datetime(2026, 8, 1, 12, 0, 0),
                ),
            ),
        )

    fp_a = compute_source_snapshot_fingerprint(make(_sha("a")))
    fp_b = compute_source_snapshot_fingerprint(make(_sha("b")))
    assert fp_a != fp_b


def test_case_fingerprint_excludes_label() -> None:
    company = uuid4()
    c1 = _case(company_id=company, label_fp=None)
    c2 = _case(company_id=company, label_fp=_sha("c"))
    assert compute_eval_case_fingerprint(c1) == compute_eval_case_fingerprint(c2)


def test_execution_spec_fingerprint_excludes_label() -> None:
    company = uuid4()
    config = _execution_config()
    case = _case(company_id=company, label_fp=None)
    spec = EvalExecutionSpec(
        case_fingerprint=compute_eval_case_fingerprint(case),
        source_snapshot_fingerprint=_sha("b"),
        execution_config_fingerprint=compute_execution_config_fingerprint(config),
        variant_id=EvalVariantId.INSIGHTFORGE_FULL,
    )
    fp1 = compute_execution_spec_fingerprint(spec)

    # 只改 label（scoring 侧），execution spec fp 不变
    case2 = _case(company_id=company, label_fp=_sha("c"))
    spec2 = EvalExecutionSpec(
        case_fingerprint=compute_eval_case_fingerprint(case2),
        source_snapshot_fingerprint=_sha("b"),
        execution_config_fingerprint=compute_execution_config_fingerprint(config),
        variant_id=EvalVariantId.INSIGHTFORGE_FULL,
    )
    assert compute_execution_spec_fingerprint(spec2) == fp1


def test_scoring_spec_fingerprint_includes_label() -> None:
    base = EvalScoringSpec(
        execution_result_fingerprint=_sha("a"),
        human_label_fingerprint=_sha("b"),
    )
    changed = EvalScoringSpec(
        execution_result_fingerprint=_sha("a"),
        human_label_fingerprint=_sha("c"),
    )
    assert compute_scoring_spec_fingerprint(base) != compute_scoring_spec_fingerprint(changed)


def test_human_label_fingerprint_excludes_annotation() -> None:
    label = HumanLabel(
        case_id="acme-2024",
        case_version=1,
        label_version=1,
        financial_facts=(
            FinancialFactLabel(
                metric_code="net_profit",
                period="FY2024",
                unit="cny_yuan",
                expected_value=Decimal("1"),
            ),
        ),
        annotation="note A",
    )
    label2 = label.model_copy(update={"annotation": "note B"})
    assert compute_human_label_fingerprint(label) == compute_human_label_fingerprint(label2)


def test_human_label_fingerprint_sensitive_to_semantic() -> None:
    def make(value: str) -> HumanLabel:
        return HumanLabel(
            case_id="acme-2024",
            case_version=1,
            label_version=1,
            financial_facts=(
                FinancialFactLabel(
                    metric_code="net_profit",
                    period="FY2024",
                    unit="cny_yuan",
                    expected_value=Decimal(value),
                ),
            ),
        )

    assert compute_human_label_fingerprint(make("1")) != compute_human_label_fingerprint(make("2"))


def test_variant_output_fingerprint_sensitive() -> None:
    o1 = EvalVariantOutput(
        variant_id=EvalVariantId.SINGLE_RAG,
        case_id="acme-2024",
        case_version=1,
        final_text="报告正文",
    )
    o2 = EvalVariantOutput(
        variant_id=EvalVariantId.SINGLE_RAG,
        case_id="acme-2024",
        case_version=1,
        final_text="不同正文",
    )
    assert compute_variant_output_fingerprint(o1) == compute_variant_output_fingerprint(o1)
    assert compute_variant_output_fingerprint(o1) != compute_variant_output_fingerprint(o2)


def _config_with_components(*components: EvalComponentVersion) -> EvalExecutionConfig:
    return EvalExecutionConfig(
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
        component_versions=components,
    )


def test_execution_config_fingerprint_includes_component_version() -> None:
    c1 = _config_with_components(
        EvalComponentVersion(component_name="audit", component_version="v1")
    )
    c2 = _config_with_components(
        EvalComponentVersion(component_name="audit", component_version="v2")
    )
    assert compute_execution_config_fingerprint(c1) != compute_execution_config_fingerprint(c2)


def test_execution_config_fingerprint_component_order_independent() -> None:
    c1 = _config_with_components(
        EvalComponentVersion(component_name="audit", component_version="v1"),
        EvalComponentVersion(component_name="evidence_extractor", component_version="v2"),
    )
    c2 = _config_with_components(
        EvalComponentVersion(component_name="evidence_extractor", component_version="v2"),
        EvalComponentVersion(component_name="audit", component_version="v1"),
    )
    assert compute_execution_config_fingerprint(c1) == compute_execution_config_fingerprint(c2)
