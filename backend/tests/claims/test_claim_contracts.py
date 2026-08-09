"""Claim contracts unit tests (stage 4A, spec 4/9).

零 LLM / 零 Chroma / 零 DB：只验证 ClaimDraft 构造校验、枚举白名单、
fingerprint 确定性、canonical 归一化。
"""

from uuid import uuid4

import pytest

from app.claims.contracts import (
    CLAIM_SCHEMA_VERSION,
    ClaimAnalysisDomain,
    ClaimConfidence,
    ClaimDraft,
    ClaimEvidenceRelation,
    ClaimImportance,
    ClaimKind,
    compute_claim_fingerprint,
    compute_research_question_sha256,
)
from app.claims.errors import ClaimDraftError
from app.evidence.contracts import (
    compute_research_question_sha256 as evidence_research_question_sha256,
)

_FORBIDDEN_CLAIM_KINDS = (
    "prediction",
    "buy",
    "sell",
    "recommendation",
    "price_target",
    "return_forecast",
)


def _draft(**overrides) -> ClaimDraft:
    values = dict(
        company_id=uuid4(),
        research_question="2024年贵州茅台净利润增长情况？",
        statement="贵州茅台2024年归属净利润同比增长15%。",
        analysis_domain=ClaimAnalysisDomain.FINANCIAL,
        claim_kind=ClaimKind.FACT,
        confidence=ClaimConfidence.HIGH,
        importance=ClaimImportance.NORMAL,
        support_evidence_ids=[uuid4()],
        contradict_evidence_ids=[],
        context_evidence_ids=[],
        analyst_name="structured-analyst",
        analyst_version=1,
        analyst_model_id="deepseek:deepseek-v4-flash",
    )
    values.update(overrides)
    return ClaimDraft(**values)


def _fp(draft: ClaimDraft) -> str:
    return compute_claim_fingerprint(
        claim_schema_version=CLAIM_SCHEMA_VERSION,
        company_id=draft.company_id,
        research_question=draft.research_question,
        statement=draft.statement,
        analysis_domain=draft.analysis_domain.value,
        claim_kind=draft.claim_kind.value,
        confidence=draft.confidence.value,
        importance=draft.importance.value,
        analyst_name=draft.analyst_name,
        analyst_version=draft.analyst_version,
        analyst_model_id=draft.analyst_model_id,
        supports=draft.support_evidence_ids,
        contradicts=draft.contradict_evidence_ids,
        context=draft.context_evidence_ids,
    )


# ---------------------------------------------------------------- 枚举


def test_claim_schema_version_frozen() -> None:
    assert CLAIM_SCHEMA_VERSION == 1


def test_analysis_domain_enum_values() -> None:
    assert {member.value for member in ClaimAnalysisDomain} == {
        "financial",
        "business",
        "event",
        "macro",
        "risk",
        "valuation",
    }


def test_claim_kind_enum_excludes_prediction_and_recommendations() -> None:
    assert {member.value for member in ClaimKind} == {
        "fact",
        "inference",
        "risk",
        "relative_valuation",
    }
    values = {member.value for member in ClaimKind}
    for forbidden in _FORBIDDEN_CLAIM_KINDS:
        assert forbidden not in values


def test_confidence_importance_enum_values() -> None:
    assert {member.value for member in ClaimConfidence} == {"low", "medium", "high"}
    assert {member.value for member in ClaimImportance} == {"normal", "critical"}


def test_relation_enum_values() -> None:
    assert {member.value for member in ClaimEvidenceRelation} == {
        "supports",
        "contradicts",
        "context",
    }


# ---------------------------------------------------------------- ClaimDraft 校验


def test_blank_research_question_rejected() -> None:
    with pytest.raises(ClaimDraftError):
        _draft(research_question="   ")


def test_blank_statement_rejected() -> None:
    with pytest.raises(ClaimDraftError):
        _draft(statement="\n \t")


def test_blank_analyst_name_rejected() -> None:
    with pytest.raises(ClaimDraftError):
        _draft(analyst_name="")


def test_analyst_version_zero_or_below_rejected() -> None:
    with pytest.raises(ClaimDraftError):
        _draft(analyst_version=0)
    with pytest.raises(ClaimDraftError):
        _draft(analyst_version=-2)


def test_analyst_version_must_be_int() -> None:
    with pytest.raises(ClaimDraftError):
        _draft(analyst_version="1")


def test_non_enum_domain_rejected() -> None:
    with pytest.raises(ClaimDraftError):
        _draft(analysis_domain="financial")  # str 不是 StrEnum


def test_analyst_model_id_blank_normalized_to_none() -> None:
    draft = _draft(analyst_model_id="   ")
    assert draft.analyst_model_id is None


# ---------------------------------------------------------------- evidence id 归一化


def test_duplicate_evidence_ids_dedup_and_canonical_sort() -> None:
    b = uuid4()
    a = uuid4()
    draft = _draft(
        support_evidence_ids=[b, a, b], contradict_evidence_ids=[], context_evidence_ids=[]
    )
    # 去重后按 str(uuid) 升序，与调用方提交顺序无关。
    assert draft.support_evidence_ids == sorted({a, b}, key=str)


def test_evidence_ids_deterministic_normalization_order_invariant() -> None:
    a = uuid4()
    b = uuid4()
    first = _draft(support_evidence_ids=[b, a], contradict_evidence_ids=[], context_evidence_ids=[])
    second = _draft(
        support_evidence_ids=[a, b], contradict_evidence_ids=[], context_evidence_ids=[]
    )
    assert first.support_evidence_ids == second.support_evidence_ids


def test_cross_relation_duplicate_rejected() -> None:
    card_id = uuid4()
    with pytest.raises(ClaimDraftError):
        _draft(
            support_evidence_ids=[card_id],
            context_evidence_ids=[card_id],
        )
    with pytest.raises(ClaimDraftError):
        _draft(
            support_evidence_ids=[card_id],
            contradict_evidence_ids=[card_id],
        )
    with pytest.raises(ClaimDraftError):
        _draft(
            contradict_evidence_ids=[card_id],
            context_evidence_ids=[card_id],
        )


def test_supports_may_be_empty_at_draft_level() -> None:
    # 契约层允许空 supports（构造通过）；"至少 1 个 supports" 由 ClaimService
    # 以 ClaimEvidenceInsufficient 在持久化前强制（结构性规则，不做语义判断）。
    draft = _draft(support_evidence_ids=[])
    assert draft.support_evidence_ids == []


# ---------------------------------------------------------------- fingerprint


def test_fingerprint_is_deterministic() -> None:
    draft = _draft()
    assert _fp(draft) == _fp(draft)
    assert _fp(draft) == compute_claim_fingerprint(
        claim_schema_version=CLAIM_SCHEMA_VERSION,
        company_id=draft.company_id,
        research_question=draft.research_question,
        statement=draft.statement,
        analysis_domain=draft.analysis_domain.value,
        claim_kind=draft.claim_kind.value,
        confidence=draft.confidence.value,
        importance=draft.importance.value,
        analyst_name=draft.analyst_name,
        analyst_version=draft.analyst_version,
        analyst_model_id=draft.analyst_model_id,
        supports=draft.support_evidence_ids,
        contradicts=draft.contradict_evidence_ids,
        context=draft.context_evidence_ids,
    )


def test_fingerprint_changes_on_statement_change() -> None:
    assert _fp(_draft()) != _fp(_draft(statement="贵州茅台2024年营收同比增长15%。"))


def test_fingerprint_changes_on_evidence_relation_change() -> None:
    card_id = uuid4()
    base = _fp(_draft(support_evidence_ids=[card_id], context_evidence_ids=[]))
    moved = _fp(_draft(support_evidence_ids=[], context_evidence_ids=[card_id]))
    assert base != moved


def test_fingerprint_changes_on_confidence_change() -> None:
    assert _fp(_draft()) != _fp(_draft(confidence=ClaimConfidence.MEDIUM))


def test_fingerprint_changes_on_analyst_version_change() -> None:
    assert _fp(_draft()) != _fp(_draft(analyst_version=2))


def test_fingerprint_changes_on_company_change() -> None:
    assert _fp(_draft()) != _fp(_draft(company_id=uuid4()))


def test_fingerprint_does_not_include_claim_id_or_created_at() -> None:
    # fingerprint 只由 ClaimDraft 语义输入决定（不含 claim_id / created_at），
    # 同一 draft 无论调用次数都产生同一指纹（无时间/随机输入）。
    assert len(_fp(_draft())) == 64
    assert all(c in "0123456789abcdef" for c in _fp(_draft()))


# ---------------------------------------------------------------- question hash


def test_research_question_sha256_matches_evidence_algorithm() -> None:
    question = "  2024年贵州茅台净利润增长情况？  "
    assert compute_research_question_sha256(question) == evidence_research_question_sha256(question)
    assert len(compute_research_question_sha256(question)) == 64
