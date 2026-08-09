"""Evidence card contracts + fingerprint unit tests (stage 3C.1).

校验 EvidenceCardDraft 的输入防御（只允许语义输入、trim 归一化、类型 /
区间 / 版本约束）、research_question trim + sha256、EvidenceType /
EvidenceConfidence 枚举、quote 范围，以及 compute_evidence_fingerprint 的
确定性 / 敏感性（语义、quote、extractor 版本任一变化 → 新指纹）。
"""

import hashlib
from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from app.evidence.contracts import (
    EVIDENCE_SCHEMA_VERSION,
    EvidenceCardDraft,
    EvidenceConfidence,
    EvidenceOrigin,
    EvidenceType,
    compute_evidence_fingerprint,
    compute_research_question_sha256,
)
from app.evidence.errors import EvidenceCardDraftError

_CHUNK_ID = UUID("11111111-1111-1111-1111-111111111111")
_COMPANY_ID = UUID("22222222-2222-2222-2222-222222222222")
_SOURCE_ID = UUID("33333333-3333-3333-3333-333333333333")
_PS_ID = UUID("44444444-4444-4444-4444-444444444444")
_CS_ID = UUID("55555555-5555-5555-5555-555555555555")

_QUESTION = "2024年贵州茅台净利润增长情况？"
_STATEMENT = "2024年贵州茅台归属净利润同比增长15%。"
_PUBLISHED_AT = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)
_PERIOD_END = date(2024, 12, 31)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _draft(**overrides) -> EvidenceCardDraft:
    values = dict(
        research_question=_QUESTION,
        evidence_statement=_STATEMENT,
        evidence_type=EvidenceType.METRIC,
        chunk_id=_CHUNK_ID,
        quote_start=0,
        quote_end=10,
        extractor_name="test-extractor",
        extractor_version=1,
        extractor_model_id="test-model",
        extractor_confidence=EvidenceConfidence.HIGH,
    )
    values.update(overrides)
    return EvidenceCardDraft(**values)


def _fingerprint(**overrides) -> str:
    values = dict(
        evidence_schema_version=EVIDENCE_SCHEMA_VERSION,
        origin_type=EvidenceOrigin.DOCUMENT_CHUNK.value,
        company_id=_COMPANY_ID,
        source_id=_SOURCE_ID,
        parsed_source_id=_PS_ID,
        chunk_set_id=_CS_ID,
        chunk_id=_CHUNK_ID,
        research_question=_QUESTION,
        evidence_statement=_STATEMENT,
        evidence_type=EvidenceType.METRIC.value,
        quote_start=0,
        quote_end=10,
        quote_sha256=_sha("净利润增长"),
        locator_refs=[{"block_ordinal": 1, "char_start": 0, "char_end": 10}],
        provider_key="xinhuanet",
        source_published_at=_PUBLISHED_AT,
        reporting_period_end=_PERIOD_END,
        authority_tier_snapshot=3,
        critical_claim_eligible_snapshot=False,
        extractor_name="test-extractor",
        extractor_version=1,
        extractor_model_id="test-model",
        extractor_confidence=EvidenceConfidence.HIGH.value,
    )
    values.update(overrides)
    return compute_evidence_fingerprint(**values)


# ---------------------------------------------------------------- draft 校验


def test_draft_trim_normalizes_research_question_and_statement() -> None:
    draft = _draft(research_question=f"  {_QUESTION}  ", evidence_statement=f"  {_STATEMENT}  ")
    assert draft.research_question == _QUESTION
    assert draft.evidence_statement == _STATEMENT


def test_draft_rejects_blank_research_question() -> None:
    with pytest.raises(EvidenceCardDraftError):
        _draft(research_question="   ")


def test_draft_rejects_blank_evidence_statement() -> None:
    with pytest.raises(EvidenceCardDraftError):
        _draft(evidence_statement="\t")


def test_draft_rejects_str_evidence_type() -> None:
    with pytest.raises(EvidenceCardDraftError):
        _draft(evidence_type="metric")  # 必须传 EvidenceType 枚举


def test_draft_rejects_str_confidence() -> None:
    with pytest.raises(EvidenceCardDraftError):
        _draft(extractor_confidence="high")  # 必须传 EvidenceConfidence 枚举


def test_draft_rejects_non_uuid_chunk_id() -> None:
    with pytest.raises(EvidenceCardDraftError):
        _draft(chunk_id="not-a-uuid")


def test_draft_rejects_negative_quote_start() -> None:
    with pytest.raises(EvidenceCardDraftError):
        _draft(quote_start=-1, quote_end=10)


def test_draft_rejects_quote_end_lte_start() -> None:
    with pytest.raises(EvidenceCardDraftError):
        _draft(quote_start=5, quote_end=5)


def test_draft_rejects_blank_extractor_name() -> None:
    with pytest.raises(EvidenceCardDraftError):
        _draft(extractor_name="")


def test_draft_rejects_extractor_version_zero() -> None:
    with pytest.raises(EvidenceCardDraftError):
        _draft(extractor_version=0)


def test_draft_normalizes_empty_extractor_model_id_to_none() -> None:
    assert _draft(extractor_model_id="  ").extractor_model_id is None
    assert _draft(extractor_model_id=None).extractor_model_id is None


def test_draft_has_no_provenance_fields() -> None:
    """调用方不得提供 provenance / quote_text / locator_refs 字段。"""
    fields = set(EvidenceCardDraft.__dataclass_fields__)
    for forbidden in (
        "company_id",
        "source_id",
        "parsed_source_id",
        "chunk_set_id",
        "authority_tier_snapshot",
        "provider_key",
        "source_published_at",
        "reporting_period_end",
        "locator_refs",
        "quote_text",
        "quote_sha256",
        "evidence_fingerprint",
    ):
        assert forbidden not in fields


# ---------------------------------------------------------------- question hash


def test_research_question_sha256_uses_trimmed_utf8() -> None:
    assert compute_research_question_sha256(f"  {_QUESTION}  ") == _sha(_QUESTION)
    assert compute_research_question_sha256(_QUESTION) == _sha(_QUESTION)


def test_research_question_sha256_is_64_lowercase_hex() -> None:
    digest = compute_research_question_sha256(_QUESTION)
    assert len(digest) == 64
    assert digest == digest.lower()


# ---------------------------------------------------------------- enums


def test_evidence_type_values_frozen() -> None:
    assert {t.value for t in EvidenceType} == {
        "fact",
        "metric",
        "event",
        "statement",
        "context",
    }


def test_evidence_confidence_values_frozen() -> None:
    assert {c.value for c in EvidenceConfidence} == {"low", "medium", "high"}


def test_schema_version_is_two() -> None:
    # v2 = 泛化 origin 模型（stage 3C.3A）：document fingerprint 加入 origin_type。
    assert EVIDENCE_SCHEMA_VERSION == 2


# ---------------------------------------------------------------- fingerprint


def test_fingerprint_is_deterministic() -> None:
    assert _fingerprint() == _fingerprint()


def test_fingerprint_is_64_lowercase_hex() -> None:
    digest = _fingerprint()
    assert len(digest) == 64
    assert digest == digest.lower()


def test_fingerprint_sensitive_to_evidence_statement() -> None:
    assert _fingerprint(evidence_statement="不同表述") != _fingerprint()


def test_fingerprint_sensitive_to_research_question() -> None:
    assert _fingerprint(research_question="不同问题") != _fingerprint()


def test_fingerprint_sensitive_to_evidence_type() -> None:
    assert _fingerprint(evidence_type=EvidenceType.FACT.value) != _fingerprint()


def test_fingerprint_sensitive_to_quote_range() -> None:
    assert _fingerprint(quote_start=1, quote_end=9) != _fingerprint()


def test_fingerprint_sensitive_to_quote_sha256() -> None:
    assert _fingerprint(quote_sha256=_sha("另一段")) != _fingerprint()


def test_fingerprint_sensitive_to_locator_refs() -> None:
    refs = [{"block_ordinal": 1, "char_start": 2, "char_end": 10}]
    assert _fingerprint(locator_refs=refs) != _fingerprint()


def test_fingerprint_sensitive_to_extractor_version() -> None:
    assert _fingerprint(extractor_version=2) != _fingerprint()


def test_fingerprint_sensitive_to_extractor_model_id() -> None:
    assert _fingerprint(extractor_model_id="other-model") != _fingerprint()


def test_fingerprint_sensitive_to_extractor_confidence() -> None:
    assert _fingerprint(extractor_confidence=EvidenceConfidence.LOW.value) != _fingerprint()


def test_fingerprint_sensitive_to_provenance_snapshots() -> None:
    assert _fingerprint(authority_tier_snapshot=1) != _fingerprint()
    assert _fingerprint(critical_claim_eligible_snapshot=True) != _fingerprint()
    assert _fingerprint(source_published_at=None) != _fingerprint()
    assert _fingerprint(reporting_period_end=None) != _fingerprint()
    assert _fingerprint(provider_key="sse") != _fingerprint()


def test_fingerprint_sensitive_to_schema_version() -> None:
    assert _fingerprint(evidence_schema_version=3) != _fingerprint()


def test_fingerprint_sensitive_to_origin_type() -> None:
    assert _fingerprint(origin_type="macro_observation") != _fingerprint()


def test_fingerprint_sensitive_to_provenance_ids() -> None:
    assert _fingerprint(chunk_id=UUID("99999999-9999-9999-9999-999999999999")) != _fingerprint()
