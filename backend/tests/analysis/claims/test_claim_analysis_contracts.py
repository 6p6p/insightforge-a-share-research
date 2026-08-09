"""Claim analysis contracts + normalization unit tests (stage 4B.1)。

校验：
- ClaimAnalysisRequest 输入防御：company_id UUID、research_question trim 非空、
  evidence_card_ids 1..MAX_EVIDENCE_PER_REQUEST 去重 + canonical 排序、
  unsupported domain → ClaimAnalysisDomainNotReady；
- normalize_evidence_card_ids 确定性（与提交顺序无关）；
- ClaimCandidate：≥1 support_ref、E<number> 格式、无 relative_valuation、
  组内无重复、statement trim 非空；
- ClaimAnalysisDecision：relevant=false → 空 claims；relevant=true → 1..5 claims；
  reason_code 仅用于非相关；无完全重复 Claim。

**零真实 LLM**：全部只构造 Pydantic / dataclass 对象。
"""

from uuid import UUID

import pytest

from app.analysis.claims.contracts import (
    CLAIM_ANALYST_NAME,
    CLAIM_ANALYST_VERSION,
    MAX_CLAIMS_PER_DECISION,
    MAX_EVIDENCE_PER_REQUEST,
    ClaimAnalysisDecision,
    ClaimAnalysisReason,
    ClaimAnalysisRequest,
    ClaimCandidate,
    normalize_evidence_card_ids,
)
from app.analysis.claims.errors import ClaimAnalysisDomainNotReady, ClaimAnalysisInputError
from app.claims.contracts import (
    ClaimAnalysisDomain,
    ClaimConfidence,
    ClaimImportance,
    ClaimKind,
)

_COMPANY_ID = UUID("11111111-1111-1111-1111-111111111111")
_E_A = UUID("aaaaaaaa-1111-1111-1111-111111111111")
_E_B = UUID("bbbbbbbb-2222-2222-2222-222222222222")
_E_C = UUID("cccccccc-3333-3333-3333-333333333333")

_QUESTION = "2024年公司海外业务增长情况？"


def _candidate(**overrides) -> ClaimCandidate:
    values = dict(
        statement="海外业务是公司2024年收入增长的重要驱动因素",
        claim_kind=ClaimKind.INFERENCE,
        confidence=ClaimConfidence.MEDIUM,
        importance=ClaimImportance.NORMAL,
        support_refs=["E1"],
        contradict_refs=[],
        context_refs=[],
    )
    values.update(overrides)
    return ClaimCandidate(**values)


def _request(**overrides) -> ClaimAnalysisRequest:
    values = dict(
        company_id=_COMPANY_ID,
        research_question=_QUESTION,
        analysis_domain=ClaimAnalysisDomain.BUSINESS,
        evidence_card_ids=[_E_B, _E_A],
    )
    values.update(overrides)
    return ClaimAnalysisRequest(**values)


# ---------------------------------------------------------------- request 校验


def test_request_trim_and_canonical_normalization() -> None:
    request = _request(research_question=f"  {_QUESTION}  ", evidence_card_ids=[_E_B, _E_A, _E_B])
    assert request.research_question == _QUESTION
    # 去重 + 按 str(uuid) 升序（与提交顺序无关）。
    assert request.evidence_card_ids == sorted([_E_A, _E_B], key=str)


def test_request_rejects_blank_research_question() -> None:
    with pytest.raises(ClaimAnalysisInputError):
        _request(research_question="   ")


def test_request_rejects_non_uuid_company() -> None:
    with pytest.raises(ClaimAnalysisInputError):
        _request(company_id="not-a-uuid")


def test_request_rejects_empty_evidence_list() -> None:
    with pytest.raises(ClaimAnalysisInputError):
        _request(evidence_card_ids=[])


def test_request_rejects_too_many_evidence() -> None:
    too_many = [
        UUID(f"{i:08d}-0000-0000-0000-000000000000") for i in range(1, MAX_EVIDENCE_PER_REQUEST + 2)
    ]
    with pytest.raises(ClaimAnalysisInputError):
        _request(evidence_card_ids=too_many)


def test_request_rejects_non_uuid_evidence() -> None:
    with pytest.raises(ClaimAnalysisInputError):
        _request(evidence_card_ids=[_E_A, "not-a-uuid"])


def test_request_unsupported_domain_not_ready() -> None:
    for domain in (
        ClaimAnalysisDomain.FINANCIAL,
        ClaimAnalysisDomain.MACRO,
        ClaimAnalysisDomain.VALUATION,
    ):
        with pytest.raises(ClaimAnalysisDomainNotReady):
            _request(analysis_domain=domain)


def test_request_supported_domains_accepted() -> None:
    for domain in (
        ClaimAnalysisDomain.BUSINESS,
        ClaimAnalysisDomain.EVENT,
        ClaimAnalysisDomain.RISK,
    ):
        request = _request(analysis_domain=domain)
        assert request.analysis_domain == domain


def test_normalize_evidence_card_ids_deterministic() -> None:
    a = normalize_evidence_card_ids([_E_B, _E_A, _E_B])
    b = normalize_evidence_card_ids([_E_A, _E_B])
    assert a == b == sorted([_E_A, _E_B], key=str)


def test_normalize_rejects_non_list() -> None:
    with pytest.raises(ClaimAnalysisInputError):
        normalize_evidence_card_ids((_E_A, _E_B))  # type: ignore[arg-type]


# ---------------------------------------------------------------- constants


def test_analyst_identity_constants_frozen() -> None:
    assert CLAIM_ANALYST_NAME == "structured_claim_analyst"
    assert CLAIM_ANALYST_VERSION == 1
    assert MAX_CLAIMS_PER_DECISION == 5
    assert MAX_EVIDENCE_PER_REQUEST == 30


# ---------------------------------------------------------------- candidate 校验


def test_candidate_accepts_minimal_valid() -> None:
    candidate = _candidate()
    assert candidate.statement == "海外业务是公司2024年收入增长的重要驱动因素"
    assert candidate.support_refs == ["E1"]


def test_candidate_accepts_blank_padded_statement() -> None:
    # ClaimCandidate 只校验非空；trim 由 ClaimDraft 层完成（persisted 前归一化）。
    candidate = _candidate(statement="  海外业务增长。  ")
    assert candidate.statement.strip() == "海外业务增长。"


def test_candidate_rejects_blank_statement() -> None:
    with pytest.raises(ValueError):
        _candidate(statement="   ")


def test_candidate_rejects_no_support_ref() -> None:
    with pytest.raises(ValueError):
        _candidate(support_refs=[])


def test_candidate_rejects_bad_ref_format() -> None:
    with pytest.raises(ValueError):
        _candidate(support_refs=["e1"])  # 必须 E<number>（大写 E）
    with pytest.raises(ValueError):
        _candidate(support_refs=["E"])  # 必须至少 1 位数字
    with pytest.raises(ValueError):
        _candidate(support_refs=["abc"])


def test_candidate_rejects_duplicate_support_ref() -> None:
    with pytest.raises(ValueError):
        _candidate(support_refs=["E1", "E1"])


def test_candidate_rejects_relative_valuation_kind() -> None:
    with pytest.raises(ValueError):
        _candidate(claim_kind=ClaimKind.RELATIVE_VALUATION)


def test_candidate_has_no_analysis_domain_field() -> None:
    # analysis_domain 由 request 决定，不是 LLM 决定。
    assert "analysis_domain" not in ClaimCandidate.model_fields


# ---------------------------------------------------------------- decision 校验


def test_decision_relevant_false_requires_empty_claims() -> None:
    with pytest.raises(ValueError):
        ClaimAnalysisDecision(relevant=False, claims=[_candidate()])
    ok = ClaimAnalysisDecision(
        relevant=False, claims=[], reason_code=ClaimAnalysisReason.NOT_RELEVANT
    )
    assert ok.reason_code == ClaimAnalysisReason.NOT_RELEVANT


def test_decision_relevant_true_requires_one_to_five_claims() -> None:
    with pytest.raises(ValueError):
        ClaimAnalysisDecision(relevant=True, claims=[])
    too_many = [_candidate(statement=f"第{i}条观点") for i in range(MAX_CLAIMS_PER_DECISION + 1)]
    with pytest.raises(ValueError):
        ClaimAnalysisDecision(relevant=True, claims=too_many)


def test_decision_relevant_true_rejects_reason_code() -> None:
    with pytest.raises(ValueError):
        ClaimAnalysisDecision(
            relevant=True,
            claims=[_candidate()],
            reason_code=ClaimAnalysisReason.INSUFFICIENT_EVIDENCE,
        )


def test_decision_rejects_duplicate_claims() -> None:
    with pytest.raises(ValueError):
        ClaimAnalysisDecision(relevant=True, claims=[_candidate(), _candidate()])
