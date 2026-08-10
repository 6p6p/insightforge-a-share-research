"""Macro analysis contracts unit tests (stage 4C.1B)。

验证：
- 冻结常量（analyst 身份 / 两池上限 / 决策上限 / analysis_domain=macro）；
- MacroAnalysisRequest：trim / canonical sort / 1..20 macro drivers / 1..30
  company evidence / 两池不重叠 / non-UUID / 空 question / 去重；
- normalize_macro_evidence_ids：deterministic canonical order；
- MacroClaimCandidate：≥1 macro_driver_ref + ≥1 company_exposure_ref / M・E 编号
  格式 / fact 拒绝（只允许 inference / risk）/ 组内去重 / overclaim contract
  （observed_impact 需 ≥1 observed_effect_ref；uncertain 只能 risk + normal +
  plausible）/ 最小字段（无 UUID・analysis_domain・fingerprint・reasoning）；
- MacroAnalysisDecision：relevant=false→空 claims（reason_code 可选）、
  relevant=true→1..3 + reason_code=None、无完全重复 Claim。
"""

from datetime import date
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.analysis.macro.contracts import (
    MACRO_ANALYST_FOCUS,
    MACRO_ANALYST_NAME,
    MACRO_ANALYST_VERSION,
    MAX_CLAIMS_PER_DECISION,
    MAX_COMPANY_EVIDENCE_PER_REQUEST,
    MAX_MACRO_DRIVER_EVIDENCE_PER_REQUEST,
    MacroAnalysisDecision,
    MacroAnalysisReason,
    MacroAnalysisRequest,
    MacroClaimCandidate,
    normalize_macro_evidence_ids,
)
from app.analysis.macro.errors import MacroAnalysisInputError
from app.claims.contracts import ClaimKind
from app.claims.macro_contracts import (
    MacroChannelType,
    MacroClaimConfidence,
    MacroClaimImportance,
    MacroEffectDirection,
    MacroImpactStatus,
    MacroTimeAlignment,
)

_QUESTION = "利率上行对贵州茅台融资成本的影响？"
_ANALYSIS_AS_OF = date(2026, 8, 10)


def _uuid(n: int) -> UUID:
    """确定性 UUID（str 排序可预测）。"""
    return UUID(f"{n:08d}-0000-0000-0000-000000000000")


def _candidate(**overrides) -> MacroClaimCandidate:
    values = dict(
        statement="若利率持续上行，公司融资成本存在上升压力。",
        claim_kind=ClaimKind.RISK,
        confidence=MacroClaimConfidence.MEDIUM,
        importance=MacroClaimImportance.NORMAL,
        channel_type=MacroChannelType.FINANCING,
        effect_direction=MacroEffectDirection.HEADWIND,
        impact_status=MacroImpactStatus.PLAUSIBLE_IMPACT,
        time_alignment=MacroTimeAlignment.ALIGNED,
        macro_driver_refs=["M1"],
        company_exposure_refs=["E1"],
        observed_effect_refs=[],
        additional_support_evidence_refs=[],
        additional_contradict_evidence_refs=[],
        additional_context_evidence_refs=[],
    )
    values.update(overrides)
    return MacroClaimCandidate(**values)


def _request(**overrides) -> MacroAnalysisRequest:
    values = dict(
        company_id=_uuid(99),
        research_question=_QUESTION,
        analysis_as_of=_ANALYSIS_AS_OF,
        macro_driver_evidence_ids=[_uuid(1)],
        company_evidence_ids=[_uuid(2)],
    )
    values.update(overrides)
    return MacroAnalysisRequest(**values)


# ---------------------------------------------------------------- 冻结常量


def test_analyst_identity_frozen() -> None:
    assert MACRO_ANALYST_NAME == "structured_macro_context_analyst"
    assert MACRO_ANALYST_VERSION == 1


def test_limits_frozen() -> None:
    assert MAX_MACRO_DRIVER_EVIDENCE_PER_REQUEST == 20
    assert MAX_COMPANY_EVIDENCE_PER_REQUEST == 30
    assert MAX_CLAIMS_PER_DECISION == 3


def test_focus_declares_no_calculation_no_valuation() -> None:
    assert "不计算任何宏观指标" in MACRO_ANALYST_FOCUS
    assert "不做估值" in MACRO_ANALYST_FOCUS


# ---------------------------------------------------------------- Request


def test_request_trims_and_sorts_canonically() -> None:
    a, b, c = _uuid(30), _uuid(10), _uuid(20)
    d, e = _uuid(40), _uuid(50)
    req = MacroAnalysisRequest(
        company_id=_uuid(99),
        research_question=f"  {_QUESTION}  ",
        analysis_as_of=_ANALYSIS_AS_OF,
        macro_driver_evidence_ids=[a, b, c, a],
        company_evidence_ids=[d, e, d],
    )
    assert req.research_question == _QUESTION
    assert req.macro_driver_evidence_ids == sorted([a, b, c], key=str)
    assert req.company_evidence_ids == sorted([d, e], key=str)


def test_request_dedupes_and_sort_both_pools() -> None:
    a, b, c, d = _uuid(5), _uuid(3), _uuid(9), _uuid(7)
    req = MacroAnalysisRequest(
        company_id=_uuid(1),
        research_question=_QUESTION,
        analysis_as_of=_ANALYSIS_AS_OF,
        macro_driver_evidence_ids=[a, b, a, b],
        company_evidence_ids=[d, c, d, c],
    )
    assert req.macro_driver_evidence_ids == sorted([a, b], key=str)
    assert req.company_evidence_ids == sorted([c, d], key=str)


def test_request_rejects_blank_question() -> None:
    with pytest.raises(MacroAnalysisInputError):
        _request(research_question="   ")


def test_request_rejects_empty_macro_driver() -> None:
    with pytest.raises(MacroAnalysisInputError):
        _request(macro_driver_evidence_ids=[])


def test_request_rejects_empty_company_evidence() -> None:
    with pytest.raises(MacroAnalysisInputError):
        _request(company_evidence_ids=[])


def test_request_rejects_too_many_macro_drivers() -> None:
    with pytest.raises(MacroAnalysisInputError):
        _request(
            macro_driver_evidence_ids=[
                _uuid(i) for i in range(1, MAX_MACRO_DRIVER_EVIDENCE_PER_REQUEST + 2)
            ]
        )


def test_request_rejects_too_many_company_evidence() -> None:
    with pytest.raises(MacroAnalysisInputError):
        _request(
            company_evidence_ids=[_uuid(i) for i in range(1, MAX_COMPANY_EVIDENCE_PER_REQUEST + 2)]
        )


def test_request_rejects_non_uuid_evidence() -> None:
    with pytest.raises(MacroAnalysisInputError):
        _request(macro_driver_evidence_ids=[_uuid(1), "not-a-uuid"])  # type: ignore[list-item]


def test_request_rejects_non_uuid_company() -> None:
    with pytest.raises(MacroAnalysisInputError):
        _request(company_id="not-a-uuid")  # type: ignore[arg-type]


def test_request_rejects_overlapping_pools() -> None:
    with pytest.raises(MacroAnalysisInputError):
        _request(
            macro_driver_evidence_ids=[_uuid(1)],
            company_evidence_ids=[_uuid(1)],
        )


def test_normalize_macro_evidence_ids_is_deterministic() -> None:
    a, b, c = _uuid(30), _uuid(10), _uuid(20)
    assert normalize_macro_evidence_ids([a, b, c, a, b]) == [b, c, a]


def test_normalize_rejects_bool() -> None:
    with pytest.raises(MacroAnalysisInputError):
        normalize_macro_evidence_ids([_uuid(1), True])  # type: ignore[list-item]


def test_normalize_rejects_non_list() -> None:
    with pytest.raises(MacroAnalysisInputError):
        normalize_macro_evidence_ids((_uuid(1),))  # type: ignore[arg-type]


# ---------------------------------------------------------------- Candidate


def test_candidate_accepts_valid_minimal() -> None:
    candidate = _candidate()
    assert candidate.macro_driver_refs == ["M1"]
    assert candidate.company_exposure_refs == ["E1"]


def test_candidate_rejects_blank_statement() -> None:
    with pytest.raises(ValidationError):
        _candidate(statement="   ")


def test_candidate_rejects_no_macro_driver() -> None:
    with pytest.raises(ValidationError, match="至少 1 个 macro_driver_ref"):
        _candidate(macro_driver_refs=[])


def test_candidate_rejects_no_company_exposure() -> None:
    with pytest.raises(ValidationError, match="至少 1 个 company_exposure_ref"):
        _candidate(company_exposure_refs=[])


def test_candidate_rejects_fact_kind() -> None:
    # 宏观定量事实由 Macro Evidence 承载；Analyst 不得输出 fact Claim。
    with pytest.raises(ValidationError, match="只允许 inference / risk"):
        _candidate(claim_kind=ClaimKind.FACT)


def test_candidate_accepts_inference_and_risk() -> None:
    assert _candidate(claim_kind=ClaimKind.INFERENCE).claim_kind == ClaimKind.INFERENCE
    assert _candidate(claim_kind=ClaimKind.RISK).claim_kind == ClaimKind.RISK


def test_candidate_rejects_bad_macro_ref_format() -> None:
    for bad in ("m1", "M", "E1", "1", "M"):
        with pytest.raises(ValidationError, match="M<number>"):
            _candidate(macro_driver_refs=[bad])


def test_candidate_rejects_bad_evidence_ref_format() -> None:
    with pytest.raises(ValidationError, match="E<number>"):
        _candidate(company_exposure_refs=["M1"])
    with pytest.raises(ValidationError, match="E<number>"):
        _candidate(company_exposure_refs=["e1"])


def test_candidate_rejects_duplicate_within_group() -> None:
    with pytest.raises(ValidationError, match="组内不允许重复"):
        _candidate(macro_driver_refs=["M1", "M1"])


def test_candidate_rejects_observed_impact_without_observed_effect() -> None:
    # overclaim contract：observed_impact 必须带 ≥1 observed_effect_ref。
    with pytest.raises(ValidationError, match="observed_impact"):
        _candidate(
            impact_status=MacroImpactStatus.OBSERVED_IMPACT,
            observed_effect_refs=[],
        )


def test_candidate_accepts_observed_impact_with_observed_effect() -> None:
    candidate = _candidate(
        impact_status=MacroImpactStatus.OBSERVED_IMPACT,
        observed_effect_refs=["E2"],
    )
    assert candidate.impact_status == MacroImpactStatus.OBSERVED_IMPACT
    assert candidate.observed_effect_refs == ["E2"]


def test_candidate_rejects_uncertain_with_non_risk_kind() -> None:
    # time_alignment=uncertain 只能是 risk + normal + plausible_impact。
    with pytest.raises(ValidationError, match="uncertain"):
        _candidate(
            time_alignment=MacroTimeAlignment.UNCERTAIN,
            claim_kind=ClaimKind.INFERENCE,
        )


def test_candidate_accepts_uncertain_with_risk_normal_plausible() -> None:
    candidate = _candidate(
        time_alignment=MacroTimeAlignment.UNCERTAIN,
        claim_kind=ClaimKind.RISK,
        importance=MacroClaimImportance.NORMAL,
        impact_status=MacroImpactStatus.PLAUSIBLE_IMPACT,
    )
    assert candidate.time_alignment == MacroTimeAlignment.UNCERTAIN


def test_candidate_has_no_internal_fields() -> None:
    candidate = _candidate()
    dumped = candidate.model_dump()
    for forbidden in (
        "company_id",
        "analysis_domain",
        "fingerprint",
        "reasoning",
        "reasoning_content",
        "evidence_card_id",
    ):
        assert forbidden not in dumped
    for ref in candidate.macro_driver_refs + candidate.company_exposure_refs:
        assert "-" not in ref  # ref 是 M/E 编号（非 UUID 形态）。
        assert len(ref) < 8


# ---------------------------------------------------------------- Decision


def _decision(**overrides) -> MacroAnalysisDecision:
    values = dict(relevant=True, claims=[_candidate()], reason_code=None)
    values.update(overrides)
    return MacroAnalysisDecision(**values)


def test_decision_relevant_true_ok() -> None:
    decision = _decision()
    assert decision.relevant is True
    assert len(decision.claims) == 1
    assert decision.reason_code is None


def test_decision_relevant_false_requires_empty_claims() -> None:
    decision = MacroAnalysisDecision(
        relevant=False,
        claims=[],
        reason_code=MacroAnalysisReason.INSUFFICIENT_MACRO_EVIDENCE,
    )
    assert decision.claims == []
    assert decision.reason_code == MacroAnalysisReason.INSUFFICIENT_MACRO_EVIDENCE


def test_decision_relevant_false_with_claims_rejected() -> None:
    with pytest.raises(ValidationError, match="claims 必须为空"):
        MacroAnalysisDecision(
            relevant=False,
            claims=[_candidate()],
            reason_code=MacroAnalysisReason.NOT_RELEVANT,
        )


def test_decision_relevant_true_with_reason_code_rejected() -> None:
    with pytest.raises(ValidationError, match="reason_code 仅用于非相关"):
        _decision(reason_code=MacroAnalysisReason.NOT_RELEVANT)


def test_decision_relevant_true_zero_claims_rejected() -> None:
    with pytest.raises(ValidationError, match="1"):
        _decision(claims=[])


def test_decision_relevant_true_too_many_claims_rejected() -> None:
    claims = [
        _candidate(statement=f"陈述 {index}。") for index in range(MAX_CLAIMS_PER_DECISION + 1)
    ]
    with pytest.raises(ValidationError, match="1.."):
        _decision(claims=claims)


def test_decision_rejects_duplicate_claims() -> None:
    with pytest.raises(ValidationError, match="重复 Claim"):
        _decision(claims=[_candidate(), _candidate()])


def test_reason_codes_allow_only_analysis_outcomes() -> None:
    codes = {code.value for code in MacroAnalysisReason}
    assert codes == {
        "not_relevant",
        "insufficient_macro_evidence",
        "insufficient_company_evidence",
    }
