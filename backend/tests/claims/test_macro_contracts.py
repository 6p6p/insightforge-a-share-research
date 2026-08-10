"""Macro claim contracts unit tests (stage 4C.1A).

零 LLM / 零 Chroma / 零 DB：只验证 MacroClaimDraft 构造校验、枚举白名单、
transmission/claim fingerprint 确定性、canonical 归一化。
"""

from datetime import date
from uuid import uuid4

import pytest

from app.claims.contracts import ClaimKind
from app.claims.macro_contracts import (
    MACRO_CLAIM_SCHEMA_VERSION,
    MACRO_CLAIM_SCHEMA_VERSION_V4,
    MACRO_TRANSMISSION_SCHEMA_VERSION,
    MACRO_TRANSMISSION_SCHEMA_VERSION_V1,
    MacroChannelType,
    MacroClaimConfidence,
    MacroClaimDraft,
    MacroClaimImportance,
    MacroEffectDirection,
    MacroImpactStatus,
    MacroTimeAlignment,
    compute_macro_claim_fingerprint,
    compute_macro_transmission_fingerprint,
)
from app.claims.macro_errors import MacroClaimDraftError


def _draft(**overrides) -> MacroClaimDraft:
    values = dict(
        company_id=uuid4(),
        research_question="利率上行对贵州茅台融资成本的影响？",
        analysis_as_of=date(2026, 8, 10),
        statement="若利率持续上行，公司融资成本存在上升压力。",
        claim_kind=ClaimKind.RISK,
        confidence=MacroClaimConfidence.MEDIUM,
        importance=MacroClaimImportance.NORMAL,
        channel_type=MacroChannelType.FINANCING,
        effect_direction=MacroEffectDirection.HEADWIND,
        impact_status=MacroImpactStatus.PLAUSIBLE_IMPACT,
        time_alignment=MacroTimeAlignment.ALIGNED,
        macro_driver_evidence_ids=[uuid4()],
        company_exposure_evidence_ids=[uuid4()],
        observed_effect_evidence_ids=[],
        additional_support_evidence_ids=[],
        additional_contradict_evidence_ids=[],
        additional_context_evidence_ids=[],
        analyst_name="macro-analyst",
        analyst_version=1,
        analyst_model_id="deepseek:deepseek-v4-flash",
    )
    values.update(overrides)
    return MacroClaimDraft(**values)


def _entries(*ids) -> list[dict]:
    return [{"evidence_card_id": str(cid), "evidence_fingerprint": "0" * 64} for cid in ids]


# ---------------------------------------------------------------- Draft 校验


def test_draft_accepts_valid_macro_claim() -> None:
    draft = _draft()
    assert draft.claim_kind is ClaimKind.RISK
    assert draft.macro_driver_evidence_ids
    assert draft.company_exposure_evidence_ids


def test_draft_rejects_fact_kind() -> None:
    # macro facts 由 Macro Evidence 承载；Claim 只做 inference / risk。
    with pytest.raises(MacroClaimDraftError):
        _draft(claim_kind=ClaimKind.FACT)


def test_draft_rejects_relative_valuation_kind() -> None:
    with pytest.raises(MacroClaimDraftError):
        _draft(claim_kind=ClaimKind.RELATIVE_VALUATION)


def test_draft_accepts_inference_kind() -> None:
    draft = _draft(claim_kind=ClaimKind.INFERENCE)
    assert draft.claim_kind is ClaimKind.INFERENCE


def test_draft_requires_macro_driver() -> None:
    with pytest.raises(MacroClaimDraftError):
        _draft(macro_driver_evidence_ids=[])


def test_draft_requires_company_exposure() -> None:
    with pytest.raises(MacroClaimDraftError):
        _draft(company_exposure_evidence_ids=[])


def test_draft_rejects_analysis_as_of_not_date() -> None:
    with pytest.raises(MacroClaimDraftError):
        _draft(analysis_as_of="2026-08-10")


def test_draft_rejects_blank_statement() -> None:
    with pytest.raises(MacroClaimDraftError):
        _draft(statement="   ")


def test_draft_rejects_same_evidence_across_transmission_roles() -> None:
    shared = uuid4()
    with pytest.raises(MacroClaimDraftError):
        _draft(
            macro_driver_evidence_ids=[shared],
            company_exposure_evidence_ids=[shared],
        )


def test_draft_rejects_evidence_in_transmission_and_additional() -> None:
    shared = uuid4()
    with pytest.raises(MacroClaimDraftError):
        _draft(
            company_exposure_evidence_ids=[shared],
            additional_support_evidence_ids=[shared],
        )


def test_draft_rejects_evidence_across_additional_relations() -> None:
    shared = uuid4()
    with pytest.raises(MacroClaimDraftError):
        _draft(
            additional_support_evidence_ids=[shared],
            additional_contradict_evidence_ids=[shared],
        )


def test_draft_normalizes_evidence_ids_canonical_order() -> None:
    a, b = uuid4(), uuid4()
    draft = _draft(
        macro_driver_evidence_ids=[b, a, b],
        company_exposure_evidence_ids=[uuid4()],
    )
    # 去重 + str(uuid) 升序。
    assert draft.macro_driver_evidence_ids == sorted([a, b], key=str)


# ---------------------------------------------------------------- 枚举白名单


def test_enum_values_frozen() -> None:
    assert sorted(v.value for v in MacroChannelType) == sorted(
        [
            "revenue",
            "cost",
            "financing",
            "demand",
            "supply_chain",
            "trade_policy",
            "operations",
            "other",
        ]
    )
    assert sorted(v.value for v in MacroEffectDirection) == sorted(
        ["tailwind", "headwind", "mixed", "uncertain"]
    )
    assert sorted(v.value for v in MacroImpactStatus) == sorted(
        ["plausible_impact", "observed_impact"]
    )
    # 无 misaligned。
    assert sorted(v.value for v in MacroTimeAlignment) == sorted(["aligned", "uncertain"])
    # 当前 schema：claim=5 / transmission=2；legacy 常量冻结不改写。
    assert MACRO_CLAIM_SCHEMA_VERSION == 5
    assert MACRO_TRANSMISSION_SCHEMA_VERSION == 2
    assert MACRO_CLAIM_SCHEMA_VERSION_V4 == 4
    assert MACRO_TRANSMISSION_SCHEMA_VERSION_V1 == 1


# ---------------------------------------------------------------- Fingerprint


def test_transmission_fingerprint_deterministic() -> None:
    kwargs = dict(
        transmission_schema_version=MACRO_TRANSMISSION_SCHEMA_VERSION,
        company_id=uuid4(),
        channel_type=MacroChannelType.FINANCING.value,
        effect_direction=MacroEffectDirection.HEADWIND.value,
        impact_status=MacroImpactStatus.PLAUSIBLE_IMPACT.value,
        time_alignment=MacroTimeAlignment.ALIGNED.value,
        analysis_as_of=date(2026, 8, 10),
        macro_driver=_entries(uuid4()),
        company_exposure=_entries(uuid4()),
        observed_effect=[],
    )
    fp1 = compute_macro_transmission_fingerprint(**kwargs)
    fp2 = compute_macro_transmission_fingerprint(**kwargs)
    assert fp1 == fp2
    assert len(fp1) == 64


def test_transmission_fingerprint_changes_with_semantics() -> None:
    base = dict(
        transmission_schema_version=MACRO_TRANSMISSION_SCHEMA_VERSION,
        company_id=uuid4(),
        channel_type=MacroChannelType.FINANCING.value,
        effect_direction=MacroEffectDirection.HEADWIND.value,
        impact_status=MacroImpactStatus.PLAUSIBLE_IMPACT.value,
        time_alignment=MacroTimeAlignment.ALIGNED.value,
        analysis_as_of=date(2026, 8, 10),
        macro_driver=_entries(uuid4()),
        company_exposure=_entries(uuid4()),
        observed_effect=[],
    )
    assert compute_macro_transmission_fingerprint(
        **{**base, "channel_type": MacroChannelType.REVENUE.value}
    ) != compute_macro_transmission_fingerprint(**base)
    assert compute_macro_transmission_fingerprint(
        **{**base, "effect_direction": MacroEffectDirection.TAILWIND.value}
    ) != compute_macro_transmission_fingerprint(**base)
    assert compute_macro_transmission_fingerprint(
        **{**base, "observed_effect": _entries(uuid4())}
    ) != compute_macro_transmission_fingerprint(**base)


def test_macro_claim_fingerprint_deterministic_and_changes() -> None:
    kwargs = dict(
        claim_schema_version=MACRO_CLAIM_SCHEMA_VERSION,
        company_id=uuid4(),
        research_question="利率上行对公司融资成本的影响？",
        analysis_as_of=date(2026, 8, 10),
        statement="公司融资成本存在上升压力。",
        claim_kind=ClaimKind.RISK.value,
        confidence=MacroClaimConfidence.MEDIUM.value,
        importance=MacroClaimImportance.NORMAL.value,
        analyst_name="macro-analyst",
        analyst_version=1,
        analyst_model_id="deepseek:deepseek-v4-flash",
        transmission_fingerprint="1" * 64,
        additional_supports=[],
        additional_contradicts=[],
        additional_context=[],
    )
    fp1 = compute_macro_claim_fingerprint(**kwargs)
    fp2 = compute_macro_claim_fingerprint(**kwargs)
    assert fp1 == fp2
    assert len(fp1) == 64
    assert (
        compute_macro_claim_fingerprint(**{**kwargs, "transmission_fingerprint": "2" * 64}) != fp1
    )
    assert compute_macro_claim_fingerprint(**{**kwargs, "additional_context": [uuid4()]}) != fp1
    assert compute_macro_claim_fingerprint(**{**kwargs, "statement": "另一句。"}) != fp1


def test_fingerprints_include_schema_version_no_cross_version_collision() -> None:
    """schema version 在 fingerprint payload 中：v4/v5、v1/v2 永不误 collision。

    版本升级 = 新 fingerprint = 新 Claim + 新链，历史 v1/v4 对象原样保留。
    """
    claim_kwargs = dict(
        company_id=uuid4(),
        research_question="利率上行对公司融资成本的影响？",
        analysis_as_of=date(2026, 8, 10),
        statement="公司融资成本存在上升压力。",
        claim_kind=ClaimKind.RISK.value,
        confidence=MacroClaimConfidence.MEDIUM.value,
        importance=MacroClaimImportance.NORMAL.value,
        analyst_name="macro-analyst",
        analyst_version=1,
        analyst_model_id="deepseek:deepseek-v4-flash",
        transmission_fingerprint="1" * 64,
        additional_supports=[],
        additional_contradicts=[],
        additional_context=[],
    )
    v4 = compute_macro_claim_fingerprint(
        claim_schema_version=MACRO_CLAIM_SCHEMA_VERSION_V4, **claim_kwargs
    )
    v5 = compute_macro_claim_fingerprint(
        claim_schema_version=MACRO_CLAIM_SCHEMA_VERSION, **claim_kwargs
    )
    assert v4 != v5

    trans_kwargs = dict(
        company_id=uuid4(),
        channel_type=MacroChannelType.FINANCING.value,
        effect_direction=MacroEffectDirection.HEADWIND.value,
        impact_status=MacroImpactStatus.PLAUSIBLE_IMPACT.value,
        time_alignment=MacroTimeAlignment.ALIGNED.value,
        analysis_as_of=date(2026, 8, 10),
        macro_driver=_entries(uuid4()),
        company_exposure=_entries(uuid4()),
        observed_effect=[],
    )
    t1 = compute_macro_transmission_fingerprint(
        transmission_schema_version=MACRO_TRANSMISSION_SCHEMA_VERSION_V1, **trans_kwargs
    )
    t2 = compute_macro_transmission_fingerprint(
        transmission_schema_version=MACRO_TRANSMISSION_SCHEMA_VERSION, **trans_kwargs
    )
    assert t1 != t2


def test_transmission_fingerprint_excludes_statement_and_analyst_identity() -> None:
    """相同 transmission semantics → 相同 transmission fingerprint（不随 statement /
    analyst 变化），这是 0024 移除 global UNIQUE 的语义前提。"""
    base = dict(
        company_id=uuid4(),
        channel_type=MacroChannelType.FINANCING.value,
        effect_direction=MacroEffectDirection.HEADWIND.value,
        impact_status=MacroImpactStatus.PLAUSIBLE_IMPACT.value,
        time_alignment=MacroTimeAlignment.ALIGNED.value,
        analysis_as_of=date(2026, 8, 10),
        macro_driver=_entries(uuid4()),
        company_exposure=_entries(uuid4()),
        observed_effect=[],
    )
    fp = compute_macro_transmission_fingerprint(
        transmission_schema_version=MACRO_TRANSMISSION_SCHEMA_VERSION, **base
    )
    # payload 不含 statement / analyst_name / analyst_version / analyst_model_id /
    # claim_id / transmission_id / created_at——这些根本不作为参数传入。
    assert len(fp) == 64
    assert (
        compute_macro_transmission_fingerprint(
            transmission_schema_version=MACRO_TRANSMISSION_SCHEMA_VERSION, **base
        )
        == fp
    )
