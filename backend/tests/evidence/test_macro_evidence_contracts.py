"""Macro evidence contracts unit tests (stage 3C.3A).

校验 MacroEvidenceDraft 的输入防御（只允许语义输入、trim 归一化、类型 /
版本约束；**不得**提供 value/period/provider/snapshot/series/locator/
authority tier），compute_macro_evidence_fingerprint 的确定性 / 敏感性，
以及 build_macro_observation_locator 的 deterministic structured locator。

零网络 / 零 DB：全部是纯函数。
"""

from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest

from app.evidence.contracts import (
    EVIDENCE_SCHEMA_VERSION,
    EvidenceConfidence,
    EvidenceOrigin,
    MacroEvidenceDraft,
    build_macro_observation_locator,
    compute_macro_evidence_fingerprint,
)
from app.evidence.errors import EvidenceCardDraftError

_COMPANY_ID = UUID("11111111-1111-1111-1111-111111111111")
_OBS_ID = UUID("22222222-2222-2222-2222-222222222222")
_SNAP_ID = UUID("33333333-3333-3333-3333-333333333333")
_SERIES_ID = UUID("44444444-4444-4444-4444-444444444444")

_QUESTION = "中国2024年人口规模是多少？"
_STATEMENT = "2024年中国总人口为14.1亿人（世界银行 SP.POP.TOTL）。"
_PERIOD = "2024"


def _draft(**overrides) -> MacroEvidenceDraft:
    values = dict(
        company_id=_COMPANY_ID,
        research_question=_QUESTION,
        macro_observation_id=_OBS_ID,
        evidence_statement=_STATEMENT,
        extractor_name="macro-extractor",
        extractor_version=1,
        extractor_model_id="deepseek:deepseek-v4-flash",
        extractor_confidence=EvidenceConfidence.HIGH,
    )
    values.update(overrides)
    return MacroEvidenceDraft(**values)


def _locator(**overrides) -> list[dict]:
    values = dict(
        provider_key="world_bank",
        series_id=_SERIES_ID,
        snapshot_id=_SNAP_ID,
        observation_id=_OBS_ID,
        source_id="2",
        external_indicator_id="SP.POP.TOTL",
        geography_code="CHN",
        frequency="annual",
        period=_PERIOD,
        normalized_period_start=date(2024, 1, 1),
    )
    values.update(overrides)
    return build_macro_observation_locator(**values)


def _fingerprint(**overrides) -> str:
    values = dict(
        evidence_schema_version=EVIDENCE_SCHEMA_VERSION,
        origin_type=EvidenceOrigin.MACRO_OBSERVATION.value,
        company_id=_COMPANY_ID,
        research_question=_QUESTION,
        evidence_statement=_STATEMENT,
        evidence_type="metric",
        macro_observation_id=_OBS_ID,
        macro_snapshot_id=_SNAP_ID,
        macro_series_id=_SERIES_ID,
        period=_PERIOD,
        normalized_period_start=date(2024, 1, 1),
        value_numeric=Decimal("1410000000"),
        is_missing=False,
        provider_key="world_bank",
        authority_tier_snapshot=1,
        critical_claim_eligible_snapshot=True,
        locator_refs=_locator(),
        extractor_name="macro-extractor",
        extractor_version=1,
        extractor_model_id="deepseek:deepseek-v4-flash",
        extractor_confidence=EvidenceConfidence.HIGH.value,
    )
    values.update(overrides)
    return compute_macro_evidence_fingerprint(**values)


# ---------------------------------------------------------------- draft 校验


def test_macro_draft_trim_normalizes_text_fields() -> None:
    draft = _draft(
        research_question=f"  {_QUESTION}  ",
        evidence_statement=f"\t{_STATEMENT}\n",
        extractor_name="  macro-extractor  ",
    )
    assert draft.research_question == _QUESTION
    assert draft.evidence_statement == _STATEMENT
    assert draft.extractor_name == "macro-extractor"


def test_macro_draft_rejects_blank_research_question() -> None:
    with pytest.raises(EvidenceCardDraftError):
        _draft(research_question="   ")


def test_macro_draft_rejects_blank_evidence_statement() -> None:
    with pytest.raises(EvidenceCardDraftError):
        _draft(evidence_statement="")


def test_macro_draft_rejects_non_uuid_company_id() -> None:
    with pytest.raises(EvidenceCardDraftError):
        _draft(company_id="not-a-uuid")


def test_macro_draft_rejects_non_uuid_observation_id() -> None:
    with pytest.raises(EvidenceCardDraftError):
        _draft(macro_observation_id="not-a-uuid")


def test_macro_draft_rejects_blank_extractor_name() -> None:
    with pytest.raises(EvidenceCardDraftError):
        _draft(extractor_name="   ")


def test_macro_draft_rejects_extractor_version_zero() -> None:
    with pytest.raises(EvidenceCardDraftError):
        _draft(extractor_version=0)


def test_macro_draft_rejects_str_confidence() -> None:
    with pytest.raises(EvidenceCardDraftError):
        _draft(extractor_confidence="high")  # 必须传 EvidenceConfidence 枚举


def test_macro_draft_normalizes_empty_model_id_to_none() -> None:
    assert _draft(extractor_model_id="  ").extractor_model_id is None
    assert _draft(extractor_model_id=None).extractor_model_id is None


def test_macro_draft_excludes_provenance_and_value_fields() -> None:
    """调用方不得提供 value/period/provider/snapshot/series/locator/authority。"""
    fields = set(MacroEvidenceDraft.__dataclass_fields__)
    for forbidden in (
        "value_numeric",
        "is_missing",
        "period",
        "provider_key",
        "snapshot_id",
        "series_id",
        "locator_refs",
        "authority_tier_snapshot",
        "critical_claim_eligible_snapshot",
        "source_published_at",
        "reporting_period_end",
        "quote_text",
        "quote_sha256",
        "evidence_fingerprint",
    ):
        assert forbidden not in fields


def test_macro_draft_fixes_evidence_type_to_metric() -> None:
    """evidence_type 固定 metric：draft 无 evidence_type 字段。"""
    assert "evidence_type" not in MacroEvidenceDraft.__dataclass_fields__


# ---------------------------------------------------------------- locator


def test_macro_locator_is_single_deterministic_structured_entry() -> None:
    locator = _locator()
    assert len(locator) == 1
    assert locator[0] == locator[0]
    entry = locator[0]
    assert entry["type"] == "macro_observation"
    assert entry["provider_key"] == "world_bank"
    assert entry["series_id"] == str(_SERIES_ID)
    assert entry["snapshot_id"] == str(_SNAP_ID)
    assert entry["observation_id"] == str(_OBS_ID)
    assert entry["period"] == _PERIOD
    assert entry["external_indicator_id"] == "SP.POP.TOTL"
    assert entry["geography_code"] == "CHN"
    assert entry["frequency"] == "annual"


def test_macro_locator_sensitive_to_period() -> None:
    assert _locator(period="2023") != _locator()


# ---------------------------------------------------------------- fingerprint


def test_macro_fingerprint_is_deterministic() -> None:
    assert _fingerprint() == _fingerprint()


def test_macro_fingerprint_is_64_lowercase_hex() -> None:
    digest = _fingerprint()
    assert len(digest) == 64
    assert digest == digest.lower()


def test_macro_fingerprint_sensitive_to_evidence_statement() -> None:
    assert _fingerprint(evidence_statement="不同表述") != _fingerprint()


def test_macro_fingerprint_sensitive_to_research_question() -> None:
    assert _fingerprint(research_question="不同问题") != _fingerprint()


def test_macro_fingerprint_sensitive_to_extractor_version() -> None:
    assert _fingerprint(extractor_version=2) != _fingerprint()


def test_macro_fingerprint_sensitive_to_extractor_confidence() -> None:
    assert _fingerprint(extractor_confidence="low") != _fingerprint()


def test_macro_fingerprint_sensitive_to_macro_identity() -> None:
    assert (
        _fingerprint(macro_observation_id=UUID("99999999-9999-9999-9999-999999999999"))
        != _fingerprint()
    )
    assert (
        _fingerprint(macro_snapshot_id=UUID("99999999-9999-9999-9999-999999999999"))
        != _fingerprint()
    )
    assert (
        _fingerprint(macro_series_id=UUID("99999999-9999-9999-9999-999999999999")) != _fingerprint()
    )
    assert _fingerprint(period="2023") != _fingerprint()


def test_macro_fingerprint_sensitive_to_value() -> None:
    assert _fingerprint(value_numeric=Decimal("1300000000")) != _fingerprint()
    assert _fingerprint(value_numeric=None, is_missing=True) != _fingerprint()


def test_macro_fingerprint_sensitive_to_provider_snapshots() -> None:
    assert _fingerprint(authority_tier_snapshot=2) != _fingerprint()
    assert _fingerprint(critical_claim_eligible_snapshot=False) != _fingerprint()
    assert _fingerprint(provider_key="nbs") != _fingerprint()


def test_macro_fingerprint_sensitive_to_locator() -> None:
    assert _fingerprint(locator_refs=[]) != _fingerprint()


def test_macro_fingerprint_sensitive_to_origin_type() -> None:
    assert _fingerprint(origin_type="document_chunk") != _fingerprint()


def test_macro_fingerprint_sensitive_to_schema_version() -> None:
    assert _fingerprint(evidence_schema_version=3) != _fingerprint()
