"""Valuation analysis contracts unit tests (stage 4C.2B.2)。

验证：
- 冻结常量（analyst 身份 / 1..3 上限 / 不估值 / 不 target price）；
- ValuationAnalysisRequest：trim / canonical sort / 1..3 comparison /
  non-UUID / 空 question / 去重；
- normalize_comparison_ids：deterministic canonical order；
- ValuationAnalysisDecision：relevant=false → assessment/confidence/importance 全
  None、refs 空、reason_code 可选；relevant=true → 三者必填、support >= 1、
  reason_code=None；V<number> 格式 / 组内去重 / 最小字段（无 company_id /
  analysis_domain / statement / fingerprint / reasoning）；
- reason_code 枚举只允许分析结果，无 prediction / recommendation / buy / sell。
"""

from datetime import date
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.analysis.valuation.contracts import (
    MAX_VALUATION_COMPARISONS_PER_REQUEST,
    MIN_VALUATION_COMPARISONS_PER_REQUEST,
    VALUATION_ANALYST_FOCUS,
    VALUATION_ANALYST_NAME,
    VALUATION_ANALYST_VERSION,
    ValuationAnalysisDecision,
    ValuationAnalysisReason,
    ValuationAnalysisRequest,
    normalize_comparison_ids,
)
from app.analysis.valuation.errors import ValuationAnalysisInputError
from app.valuation.claim_contracts import (
    ValuationClaimAssessment,
    ValuationClaimConfidence,
    ValuationClaimImportance,
)

_QUESTION = "公司当前市盈率处于什么相对水平？"
_AS_OF = date(2026, 8, 10)


def _uuid(n: int) -> UUID:
    """确定性 UUID（str 排序可预测）。"""
    return UUID(f"{n:08d}-0000-0000-0000-000000000000")


# ---------------------------------------------------------------- 冻结常量


def test_analyst_identity_frozen() -> None:
    assert VALUATION_ANALYST_NAME == "structured_relative_valuation_analyst"
    # v2 = current statement-scope-safe；v1 = historical pre-final。
    assert VALUATION_ANALYST_VERSION == 2


def test_limits_frozen() -> None:
    assert MIN_VALUATION_COMPARISONS_PER_REQUEST == 1
    assert MAX_VALUATION_COMPARISONS_PER_REQUEST == 3


def test_focus_declares_no_calculation_no_target_price() -> None:
    assert "只读不重算" in VALUATION_ANALYST_FOCUS
    assert "不选择 peers" in VALUATION_ANALYST_FOCUS
    assert "不做 target price / fair value / 买卖建议" in VALUATION_ANALYST_FOCUS


# ---------------------------------------------------------------- Request


def test_request_trims_and_sorts_canonically() -> None:
    a, b, c = _uuid(30), _uuid(10), _uuid(20)
    req = ValuationAnalysisRequest(
        company_id=_uuid(99),
        research_question=f"  {_QUESTION}  ",
        analysis_as_of=_AS_OF,
        comparison_ids=[a, b, c, a],
    )
    assert req.research_question == _QUESTION
    assert req.comparison_ids == sorted([a, b, c], key=str)


def test_request_rejects_blank_question() -> None:
    with pytest.raises(ValuationAnalysisInputError):
        ValuationAnalysisRequest(
            company_id=_uuid(1),
            research_question="   ",
            analysis_as_of=_AS_OF,
            comparison_ids=[_uuid(1)],
        )


def test_request_rejects_empty_comparisons() -> None:
    with pytest.raises(ValuationAnalysisInputError):
        ValuationAnalysisRequest(
            company_id=_uuid(1),
            research_question=_QUESTION,
            analysis_as_of=_AS_OF,
            comparison_ids=[],
        )


def test_request_rejects_too_many_comparisons() -> None:
    with pytest.raises(ValuationAnalysisInputError):
        ValuationAnalysisRequest(
            company_id=_uuid(1),
            research_question=_QUESTION,
            analysis_as_of=_AS_OF,
            comparison_ids=[_uuid(i) for i in range(1, MAX_VALUATION_COMPARISONS_PER_REQUEST + 2)],
        )


def test_request_rejects_non_uuid_comparison() -> None:
    with pytest.raises(ValuationAnalysisInputError):
        ValuationAnalysisRequest(
            company_id=_uuid(1),
            research_question=_QUESTION,
            analysis_as_of=_AS_OF,
            comparison_ids=[_uuid(1), "not-a-uuid"],  # type: ignore[list-item]
        )


def test_request_rejects_non_uuid_company() -> None:
    with pytest.raises(ValuationAnalysisInputError):
        ValuationAnalysisRequest(
            company_id="not-a-uuid",  # type: ignore[arg-type]
            research_question=_QUESTION,
            analysis_as_of=_AS_OF,
            comparison_ids=[_uuid(1)],
        )


def test_normalize_comparison_ids_is_deterministic() -> None:
    a, b, c = _uuid(30), _uuid(10), _uuid(20)
    assert normalize_comparison_ids([a, b, c, a, b]) == [b, c, a]


def test_normalize_rejects_bool() -> None:
    with pytest.raises(ValuationAnalysisInputError):
        normalize_comparison_ids([_uuid(1), True])  # type: ignore[list-item]


def test_normalize_rejects_non_list() -> None:
    with pytest.raises(ValuationAnalysisInputError):
        normalize_comparison_ids(tuple([_uuid(1)]))  # type: ignore[arg-type]


# ---------------------------------------------------------------- Decision


def _decision(**overrides) -> ValuationAnalysisDecision:
    values = dict(
        relevant=True,
        assessment=ValuationClaimAssessment.RELATIVE_HIGH,
        confidence=ValuationClaimConfidence.HIGH,
        importance=ValuationClaimImportance.NORMAL,
        support_comparison_refs=["V1"],
        contradict_comparison_refs=[],
        context_comparison_refs=[],
        reason_code=None,
    )
    values.update(overrides)
    return ValuationAnalysisDecision(**values)


def test_decision_relevant_true_ok() -> None:
    decision = _decision()
    assert decision.relevant is True
    assert decision.assessment == ValuationClaimAssessment.RELATIVE_HIGH
    assert decision.confidence == ValuationClaimConfidence.HIGH
    assert decision.importance == ValuationClaimImportance.NORMAL
    assert decision.support_comparison_refs == ["V1"]
    assert decision.reason_code is None


def test_decision_relevant_false_ok() -> None:
    decision = ValuationAnalysisDecision(
        relevant=False,
        assessment=None,
        confidence=None,
        importance=None,
        support_comparison_refs=[],
        contradict_comparison_refs=[],
        context_comparison_refs=[],
        reason_code=ValuationAnalysisReason.NOT_RELEVANT,
    )
    assert decision.reason_code == ValuationAnalysisReason.NOT_RELEVANT


def test_decision_relevant_false_with_assessment_rejected() -> None:
    with pytest.raises(ValidationError, match="必须为 None"):
        ValuationAnalysisDecision(
            relevant=False,
            assessment=ValuationClaimAssessment.RELATIVE_HIGH,
            reason_code=ValuationAnalysisReason.NOT_RELEVANT,
        )


def test_decision_relevant_false_with_refs_rejected() -> None:
    with pytest.raises(ValidationError, match="必须为空"):
        ValuationAnalysisDecision(
            relevant=False,
            support_comparison_refs=["V1"],
            reason_code=ValuationAnalysisReason.NOT_RELEVANT,
        )


def test_decision_relevant_true_without_assessment_rejected() -> None:
    with pytest.raises(ValidationError, match="必须提供"):
        _decision(assessment=None)


def test_decision_relevant_true_without_support_rejected() -> None:
    with pytest.raises(ValidationError, match="support refs >= 1"):
        _decision(support_comparison_refs=[])


def test_decision_relevant_true_with_reason_code_rejected() -> None:
    with pytest.raises(ValidationError, match="reason_code 仅用于非相关"):
        _decision(reason_code=ValuationAnalysisReason.NOT_RELEVANT)


def test_decision_rejects_bad_v_ref_format() -> None:
    # V0 / V01 格式合法（^V\d+$）；但 pack 只有 V1..Vn，不存在编号由 resolve 层
    # 作为 UnknownRef 拒绝（不做 fuzzy resolve）。
    for bad in ("v1", "V", "1", "uuid", "V 1"):
        with pytest.raises(ValidationError, match="V<number>"):
            _decision(support_comparison_refs=[bad])


def test_decision_rejects_duplicate_within_group() -> None:
    with pytest.raises(ValidationError, match="组内不允许重复"):
        _decision(support_comparison_refs=["V1", "V1"])


def test_decision_has_no_internal_fields() -> None:
    decision = _decision()
    dumped = decision.model_dump()
    for forbidden in (
        "company_id",
        "analysis_domain",
        "statement",
        "fingerprint",
        "reasoning",
        "claim_id",
        "premium",
    ):
        assert forbidden not in dumped
    # ref 是 V 编号（非 UUID），不暴露任何 UUID 形态值。
    for ref in decision.support_comparison_refs:
        assert ref.startswith("V")
        assert "-" not in ref
        assert len(ref) < 8


def test_reason_codes_allow_only_analysis_outcomes() -> None:
    codes = {code.value for code in ValuationAnalysisReason}
    assert codes == {"not_relevant", "insufficient_comparisons", "insufficient_consistency"}
    for code in codes:
        assert "prediction" not in code and "buy" not in code and "sell" not in code
