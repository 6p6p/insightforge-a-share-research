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
    FrozenCompanyIdentity,
    FrozenDocumentSourceRef,
    FrozenMacroSnapshotRef,
    FrozenModelConfig,
    FrozenSourceProviderRef,
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


def _case(company_id=None, label_fp=None) -> EvalCase:
    return EvalCase(
        case_id="acme-2024-fundamental",
        case_version=1,
        company_id=company_id if company_id is not None else uuid4(),
        company=_company(),
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
    s2 = FrozenSourceSnapshot(document_sources=(_doc_ref("a"),))
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


def test_snapshot_fingerprint_sensitive_to_document_provenance() -> None:
    """document provenance 字段（运行期被 evidence/preparation 读取）变化 → snapshot fp 变化。"""
    base = FrozenSourceSnapshot(document_sources=(_doc_ref("a"),))
    for field, value in [
        ("title", "不同标题"),
        ("source_url", "https://example.com/other"),
        ("authority_tier_snapshot", 1),
        ("critical_claim_eligible_snapshot", True),
    ]:
        changed = FrozenSourceSnapshot(document_sources=(_doc_ref("a", **{field: value}),))
        assert compute_source_snapshot_fingerprint(base) != compute_source_snapshot_fingerprint(
            changed
        ), f"{field} 变化未改变 snapshot fingerprint"
    changed_acquired = FrozenSourceSnapshot(
        document_sources=(_doc_ref("a", acquired_at=datetime(2026, 8, 2, 12, 0, 0)),)
    )
    assert compute_source_snapshot_fingerprint(base) != compute_source_snapshot_fingerprint(
        changed_acquired
    )


def test_snapshot_fingerprint_sensitive_to_provider_registry() -> None:
    """provider registry（运行期被 router / provenance 读取）变化 → snapshot fp 变化。"""

    def make(**overrides) -> FrozenSourceSnapshot:
        kwargs = dict(
            provider_key="cninfo",
            display_name="巨潮资讯",
            enabled=True,
            capabilities=("annual_report",),
        )
        kwargs.update(overrides)
        return FrozenSourceSnapshot(source_providers=(FrozenSourceProviderRef(**kwargs),))

    base = make()
    assert compute_source_snapshot_fingerprint(make()) != compute_source_snapshot_fingerprint(
        make(display_name="其他名称")
    )
    assert compute_source_snapshot_fingerprint(base) != compute_source_snapshot_fingerprint(
        make(enabled=False)
    )
    assert compute_source_snapshot_fingerprint(base) != compute_source_snapshot_fingerprint(
        make(capabilities=("news_article",))
    )


def test_provider_duplicate_capability_does_not_change_fp() -> None:
    """capabilities 是 set-like：重复 capability 去重后不改变 snapshot fingerprint。"""

    def make(caps: tuple[str, ...]) -> FrozenSourceSnapshot:
        return FrozenSourceSnapshot(
            source_providers=(
                FrozenSourceProviderRef(
                    provider_key="cninfo",
                    display_name="巨潮资讯",
                    enabled=True,
                    capabilities=caps,
                ),
            ),
        )

    base = make(("annual_report",))
    dup = make(("annual_report", "annual_report"))
    assert dup.source_providers[0].capabilities == ("annual_report",)
    assert compute_source_snapshot_fingerprint(base) == compute_source_snapshot_fingerprint(dup)


def test_case_fingerprint_sensitive_to_company_identity() -> None:
    """company identity（运行期被 planner 读取）变化 → case fp 变化。"""
    base = _case()
    for field, value in [
        ("official_name", "另一家公司"),
        ("exchange", "SZSE"),
        ("board", "szse_main"),
        ("short_name", "简称"),
        ("aliases", ("别名",)),
    ]:
        changed = base.model_copy(update={"company": _company(**{field: value})})
        assert compute_eval_case_fingerprint(base) != compute_eval_case_fingerprint(changed), (
            f"{field} 变化未改变 case fingerprint"
        )


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


def test_scoring_spec_deterministic_no_label_no_judge_valid() -> None:
    """deterministic scoring：只绑 output fp，label/judge 均为 None 也合法。"""
    spec = EvalScoringSpec(variant_output_fingerprint=_sha("a"))
    assert spec.human_label_fingerprint is None
    assert spec.judge_config_fingerprint is None
    assert compute_scoring_spec_fingerprint(spec) == compute_scoring_spec_fingerprint(spec)


def test_scoring_spec_output_fingerprint_changes_scoring_fp() -> None:
    base = EvalScoringSpec(variant_output_fingerprint=_sha("a"))
    changed = EvalScoringSpec(variant_output_fingerprint=_sha("b"))
    assert compute_scoring_spec_fingerprint(base) != compute_scoring_spec_fingerprint(changed)


def test_scoring_spec_fingerprint_includes_label() -> None:
    base = EvalScoringSpec(
        variant_output_fingerprint=_sha("a"),
        human_label_fingerprint=_sha("b"),
    )
    changed = EvalScoringSpec(
        variant_output_fingerprint=_sha("a"),
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
