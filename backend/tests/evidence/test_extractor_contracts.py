"""Evidence extractor structured-output contract unit tests (stage 3C.2).

校验 EvidenceExtractionItem / EvidenceExtractionDecision（Pydantic）：
- relevant=false → items 必须为空（reason_code 可选，仅限非相关/无证据）；
- relevant=true → items 必须 1..3 个，reason_code 必须为 None；
- 单 response 不允许完全重复 item；
- item 无 reasoning / chain_of_thought / free-form analysis 字段；
- evidence_type / confidence 枚举映射与非法值拒绝；
- 常量 EVIDENCE_EXTRACTOR_NAME / VERSION 冻结；
- EvidenceExtractionModel Protocol 结构性成立（Fake 满足）。
"""

import pytest
from pydantic import ValidationError

from app.evidence.contracts import EvidenceConfidence, EvidenceType
from app.evidence.extractor.contracts import (
    EVIDENCE_EXTRACTOR_NAME,
    EVIDENCE_EXTRACTOR_VERSION,
    MAX_EXTRACTION_ITEMS_PER_HIT,
    EvidenceExtractionDecision,
    EvidenceExtractionItem,
    EvidenceExtractionModel,
    EvidenceExtractionReason,
)
from tests.evidence.fakes import FakeEvidenceExtractionModel


def _item(**overrides) -> dict:
    base = dict(
        evidence_statement="公司2025年营业收入为100亿元。",
        evidence_type="metric",
        quote_text="公司2025年营业收入为100亿元。",
        confidence="high",
    )
    base.update(overrides)
    return base


def _decision(**overrides) -> dict:
    base = dict(
        relevant=True,
        items=[_item()],
        reason_code=None,
    )
    base.update(overrides)
    return base


def test_frozen_constants() -> None:
    assert EVIDENCE_EXTRACTOR_NAME == "structured_llm"
    assert EVIDENCE_EXTRACTOR_VERSION == 1
    assert MAX_EXTRACTION_ITEMS_PER_HIT == 3


def test_relevant_false_empty_items_with_reason_code_ok() -> None:
    decision = EvidenceExtractionDecision.model_validate(
        _decision(
            relevant=False,
            items=[],
            reason_code=EvidenceExtractionReason.NOT_RELEVANT,
        )
    )
    assert decision.relevant is False
    assert decision.items == []
    assert decision.reason_code == EvidenceExtractionReason.NOT_RELEVANT


def test_relevant_true_single_item_ok() -> None:
    decision = EvidenceExtractionDecision.model_validate(_decision())
    assert decision.relevant is True
    assert len(decision.items) == 1


def test_relevant_true_three_items_ok() -> None:
    decision = EvidenceExtractionDecision.model_validate(
        _decision(
            items=[
                _item(evidence_statement="甲", quote_text="甲"),
                _item(evidence_statement="乙", quote_text="乙"),
                _item(evidence_statement="丙", quote_text="丙"),
            ]
        )
    )
    assert len(decision.items) == 3


def test_relevant_false_with_items_rejected() -> None:
    with pytest.raises(ValidationError):
        EvidenceExtractionDecision.model_validate(_decision(relevant=False, items=[_item()]))


def test_relevant_true_with_zero_items_rejected() -> None:
    with pytest.raises(ValidationError):
        EvidenceExtractionDecision.model_validate(_decision(items=[]))


def test_relevant_true_with_four_items_rejected() -> None:
    with pytest.raises(ValidationError):
        EvidenceExtractionDecision.model_validate(
            _decision(
                items=[
                    _item(evidence_statement="一", quote_text="一"),
                    _item(evidence_statement="二", quote_text="二"),
                    _item(evidence_statement="三", quote_text="三"),
                    _item(evidence_statement="四", quote_text="四"),
                ]
            )
        )


def test_relevant_true_with_reason_code_rejected() -> None:
    with pytest.raises(ValidationError):
        EvidenceExtractionDecision.model_validate(
            _decision(reason_code=EvidenceExtractionReason.NOT_RELEVANT)
        )


def test_duplicate_item_rejected() -> None:
    with pytest.raises(ValidationError):
        EvidenceExtractionDecision.model_validate(_decision(items=[_item(), _item()]))


def test_items_differing_only_in_quote_allowed() -> None:
    # quote 可以不同（重叠/不同区间）→ 允许；完全重复才禁止。
    decision = EvidenceExtractionDecision.model_validate(
        _decision(
            items=[
                _item(evidence_statement="收入为100亿元", quote_text="收入为100亿元"),
                _item(evidence_statement="收入为100亿元", quote_text="收入为100亿元，同比增长"),
            ]
        )
    )
    assert len(decision.items) == 2


def test_blank_statement_rejected() -> None:
    with pytest.raises(ValidationError):
        EvidenceExtractionItem.model_validate(_item(evidence_statement="   "))


def test_blank_quote_rejected() -> None:
    with pytest.raises(ValidationError):
        EvidenceExtractionItem.model_validate(_item(quote_text="\t"))


def test_invalid_evidence_type_rejected() -> None:
    with pytest.raises(ValidationError):
        EvidenceExtractionItem.model_validate(_item(evidence_type="buy"))


def test_invalid_confidence_rejected() -> None:
    with pytest.raises(ValidationError):
        EvidenceExtractionItem.model_validate(_item(confidence="definitely"))


def test_type_confidence_string_coercion_to_enum() -> None:
    item = EvidenceExtractionItem.model_validate(_item())
    assert item.evidence_type is EvidenceType.METRIC
    assert item.confidence is EvidenceConfidence.HIGH


def test_quote_text_kept_verbatim_not_stripped() -> None:
    # quote_text 是逐字原文：前后空白在 schema 层**不自动 strip**（精确匹配由
    # resolver 负责；只有 strip 后非空的校验）。
    item = EvidenceExtractionItem.model_validate(_item(quote_text=" 甲  "))
    assert item.quote_text == " 甲  "


def test_missing_field_rejected() -> None:
    with pytest.raises(ValidationError):
        EvidenceExtractionDecision.model_validate({"relevant": True})


def test_no_cot_reasoning_fields() -> None:
    assert "reasoning" not in EvidenceExtractionItem.model_fields
    assert "chain_of_thought" not in EvidenceExtractionItem.model_fields
    assert "reasoning" not in EvidenceExtractionDecision.model_fields


def test_fake_satisfies_model_protocol() -> None:
    fake = FakeEvidenceExtractionModel(decision=None)
    assert isinstance(fake, EvidenceExtractionModel)
    assert fake.model_id == "fake/structured-llm@1"
