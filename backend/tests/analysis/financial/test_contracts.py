"""Financial analysis contracts unit tests (stage 4B.2C.2)。

验证：
- 冻结常量（analyst 身份 / 上限 / analysis_domain=financial）；
- FinancialAnalysisRequest：trim / canonical sort / 1..20 calcs / 0..20 evidence /
  non-UUID / 空 question / 去重；
- normalize_calculation_ids / normalize_evidence_card_ids：deterministic canonical order；
- FinancialClaimCandidate：≥1 support_calculation_ref / C<number>・E<number> 格式 /
  fact 与 relative_valuation 拒绝（只允许 inference / risk）/ 组内去重 / 最小字段
  （无 UUID・analysis_domain・formula・result_value rewrite・fingerprint・reasoning）；
- FinancialAnalysisDecision：relevant=false→空 claims（reason_code 可选）、
  relevant=true→1..3 + reason_code=None、无完全重复 Claim。
"""

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.analysis.financial.contracts import (
    FINANCIAL_ANALYST_FOCUS,
    FINANCIAL_ANALYST_NAME,
    FINANCIAL_ANALYST_VERSION,
    MAX_CALCULATIONS_PER_REQUEST,
    MAX_CLAIMS_PER_DECISION,
    MAX_EVIDENCE_PER_REQUEST,
    FinancialAnalysisDecision,
    FinancialAnalysisReason,
    FinancialAnalysisRequest,
    FinancialClaimCandidate,
    normalize_calculation_ids,
    normalize_evidence_card_ids,
)
from app.analysis.financial.errors import FinancialAnalysisInputError
from app.claims.contracts import ClaimKind
from app.claims.financial_contracts import (
    FinancialClaimConfidence,
    FinancialClaimImportance,
)

_QUESTION = "公司的经营表现如何？"


def _uuid(n: int) -> UUID:
    """确定性 UUID（str 排序可预测）。"""
    return UUID(f"{n:08d}-0000-0000-0000-000000000000")


def _candidate(**overrides) -> FinancialClaimCandidate:
    values = dict(
        statement="营业收入保持增长态势。",
        claim_kind=ClaimKind.INFERENCE,
        confidence=FinancialClaimConfidence.HIGH,
        importance=FinancialClaimImportance.NORMAL,
        support_calculation_refs=["C1"],
        contradict_calculation_refs=[],
        context_calculation_refs=[],
        additional_support_evidence_refs=[],
        additional_contradict_evidence_refs=[],
        additional_context_evidence_refs=[],
    )
    values.update(overrides)
    return FinancialClaimCandidate(**values)


# ---------------------------------------------------------------- 冻结常量


def test_analyst_identity_frozen() -> None:
    assert FINANCIAL_ANALYST_NAME == "structured_financial_analyst"
    # v2（V1.1 closure）：数字自检清单（statement 禁数字字面量）。
    assert FINANCIAL_ANALYST_VERSION == 2


def test_limits_frozen() -> None:
    assert MAX_CALCULATIONS_PER_REQUEST == 20
    assert MAX_EVIDENCE_PER_REQUEST == 20
    assert MAX_CLAIMS_PER_DECISION == 3


def test_focus_declares_no_calculation_no_valuation() -> None:
    assert "不计算任何财务指标" in FINANCIAL_ANALYST_FOCUS
    assert "不做估值" in FINANCIAL_ANALYST_FOCUS


# ---------------------------------------------------------------- Request


def test_request_trims_and_sorts_canonically() -> None:
    a, b, c = _uuid(30), _uuid(10), _uuid(20)
    req = FinancialAnalysisRequest(
        company_id=_uuid(99),
        research_question=f"  {_QUESTION}  ",
        calculation_ids=[a, b, c, a],
    )
    assert req.research_question == _QUESTION
    assert req.calculation_ids == sorted([a, b, c], key=str)
    assert req.additional_evidence_ids == []


def test_request_normalizes_additional_evidence() -> None:
    a, b = _uuid(5), _uuid(3)
    req = FinancialAnalysisRequest(
        company_id=_uuid(1),
        research_question=_QUESTION,
        calculation_ids=[a],
        additional_evidence_ids=[b, a, b],
    )
    assert req.additional_evidence_ids == sorted([a, b], key=str)


def test_request_rejects_blank_question() -> None:
    with pytest.raises(FinancialAnalysisInputError):
        FinancialAnalysisRequest(
            company_id=_uuid(1), research_question="   ", calculation_ids=[_uuid(1)]
        )


def test_request_rejects_empty_calculations() -> None:
    with pytest.raises(FinancialAnalysisInputError):
        FinancialAnalysisRequest(
            company_id=_uuid(1), research_question=_QUESTION, calculation_ids=[]
        )


def test_request_rejects_too_many_calculations() -> None:
    with pytest.raises(FinancialAnalysisInputError):
        FinancialAnalysisRequest(
            company_id=_uuid(1),
            research_question=_QUESTION,
            calculation_ids=[_uuid(i) for i in range(1, MAX_CALCULATIONS_PER_REQUEST + 2)],
        )


def test_request_rejects_too_many_evidence() -> None:
    with pytest.raises(FinancialAnalysisInputError):
        FinancialAnalysisRequest(
            company_id=_uuid(1),
            research_question=_QUESTION,
            calculation_ids=[_uuid(1)],
            additional_evidence_ids=[_uuid(i) for i in range(1, MAX_EVIDENCE_PER_REQUEST + 2)],
        )


def test_request_rejects_non_uuid_calculation() -> None:
    with pytest.raises(FinancialAnalysisInputError):
        FinancialAnalysisRequest(
            company_id=_uuid(1),
            research_question=_QUESTION,
            calculation_ids=[_uuid(1), "not-a-uuid"],  # type: ignore[list-item]
        )


def test_request_rejects_non_uuid_company() -> None:
    with pytest.raises(FinancialAnalysisInputError):
        FinancialAnalysisRequest(
            company_id="not-a-uuid",  # type: ignore[arg-type]
            research_question=_QUESTION,
            calculation_ids=[_uuid(1)],
        )


def test_normalize_calculation_ids_is_deterministic() -> None:
    a, b, c = _uuid(30), _uuid(10), _uuid(20)
    assert normalize_calculation_ids([a, b, c, a, b]) == [b, c, a]


def test_normalize_evidence_card_ids_is_deterministic() -> None:
    a, b = _uuid(30), _uuid(10)
    assert normalize_evidence_card_ids([a, b, a]) == [b, a]


def test_normalize_rejects_bool() -> None:
    with pytest.raises(FinancialAnalysisInputError):
        normalize_calculation_ids([_uuid(1), True])  # type: ignore[list-item]


def test_normalize_rejects_non_list() -> None:
    with pytest.raises(FinancialAnalysisInputError):
        normalize_calculation_ids(tuple([_uuid(1)]))  # type: ignore[arg-type]


# ---------------------------------------------------------------- Candidate


def test_candidate_accepts_valid_minimal() -> None:
    candidate = _candidate()
    assert candidate.support_calculation_refs == ["C1"]


def test_candidate_rejects_blank_statement() -> None:
    with pytest.raises(ValidationError):
        _candidate(statement="   ")


def test_candidate_rejects_no_support_calculation() -> None:
    with pytest.raises(ValidationError, match="至少 1 个 support"):
        _candidate(support_calculation_refs=[])


def test_candidate_rejects_relative_valuation_kind() -> None:
    with pytest.raises(ValidationError, match="只允许 inference / risk"):
        _candidate(claim_kind=ClaimKind.RELATIVE_VALUATION)


def test_candidate_rejects_fact_kind() -> None:
    # Financial Calculation 承担确定性定量事实；Analyst 不得输出 fact Claim。
    with pytest.raises(ValidationError, match="只允许 inference / risk"):
        _candidate(claim_kind=ClaimKind.FACT)


def test_candidate_accepts_inference_and_risk() -> None:
    candidate = _candidate(claim_kind=ClaimKind.INFERENCE)
    assert candidate.claim_kind == ClaimKind.INFERENCE
    candidate = _candidate(claim_kind=ClaimKind.RISK)
    assert candidate.claim_kind == ClaimKind.RISK


def test_candidate_rejects_bad_calc_ref_format() -> None:
    for bad in ("c1", "C", "E1", "1", "C"):
        with pytest.raises(ValidationError, match="C<number>"):
            _candidate(support_calculation_refs=[bad])


def test_candidate_rejects_bad_evidence_ref_format() -> None:
    with pytest.raises(ValidationError, match="E<number>"):
        _candidate(additional_support_evidence_refs=["C1"])
    with pytest.raises(ValidationError, match="E<number>"):
        _candidate(additional_support_evidence_refs=["e1"])


def test_candidate_rejects_duplicate_within_group() -> None:
    with pytest.raises(ValidationError, match="组内不允许重复"):
        _candidate(support_calculation_refs=["C1", "C1"])


def test_candidate_has_no_internal_fields() -> None:
    candidate = _candidate()
    dumped = candidate.model_dump()
    for forbidden in ("company_id", "analysis_domain", "formula", "fingerprint", "reasoning"):
        assert forbidden not in dumped
    # ref 是 C 编号（非 UUID），不暴露任何 UUID 形态值。
    for ref in candidate.support_calculation_refs:
        assert ref.startswith("C")
        assert "-" not in ref
        assert len(ref) < 8


# ---------------------------------------------------------------- Decision


def _decision(**overrides) -> FinancialAnalysisDecision:
    values = dict(relevant=True, claims=[_candidate()], reason_code=None)
    values.update(overrides)
    return FinancialAnalysisDecision(**values)


def test_decision_relevant_true_ok() -> None:
    decision = _decision()
    assert decision.relevant is True
    assert len(decision.claims) == 1
    assert decision.reason_code is None


def test_decision_relevant_false_requires_empty_claims() -> None:
    decision = FinancialAnalysisDecision(
        relevant=False,
        claims=[],
        reason_code=FinancialAnalysisReason.NOT_RELEVANT,
    )
    assert decision.claims == []
    assert decision.reason_code == FinancialAnalysisReason.NOT_RELEVANT


def test_decision_relevant_false_with_claims_rejected() -> None:
    with pytest.raises(ValidationError, match="claims 必须为空"):
        FinancialAnalysisDecision(
            relevant=False, claims=[_candidate()], reason_code=FinancialAnalysisReason.NOT_RELEVANT
        )


def test_decision_relevant_true_with_reason_code_rejected() -> None:
    with pytest.raises(ValidationError, match="reason_code 仅用于非相关"):
        _decision(reason_code=FinancialAnalysisReason.NOT_RELEVANT)


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
    codes = {code.value for code in FinancialAnalysisReason}
    assert codes == {"not_relevant", "insufficient_calculations", "insufficient_evidence"}


def test_request_roundtrip_uuid_integrity() -> None:
    a = uuid4()
    b = uuid4()
    req = FinancialAnalysisRequest(
        company_id=uuid4(), research_question=_QUESTION, calculation_ids=[a, b]
    )
    assert set(req.calculation_ids) == {a, b}
