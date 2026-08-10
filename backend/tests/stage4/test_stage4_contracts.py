"""Stage4WorkflowRequest contract tests (spec G + spec R: contracts).

覆盖：
- discriminated union：6 类 analysis_type 正确解析；未知 type / 类型字段混用
  被拒（pydantic ValidationError）；
- 边界：analysis_work_items 空 / >12 → 拒绝；item_id 重复 → 拒绝；
- item_id / research_question trim；
- JSON-safe：model_dump(mode="json") 可 json.dumps（UUID → str，checkpoint-safe）；
- 只放 IDs：dump 里没有 Evidence text / Calculation blob / prompt 类字段。
"""

import json
from datetime import date
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.stage4.contracts import (
    MAX_ANALYSIS_WORK_ITEMS,
    MIN_ANALYSIS_WORK_ITEMS,
    Stage4WorkflowRequest,
)

_QUESTION = "贵州茅台2026年营收与估值是否合理？"
_CUTOFF = date(2026, 8, 10)


def _generic(item_id: str = "i1", **kw) -> dict:
    values = dict(
        item_id=item_id,
        analysis_type="business",
        evidence_card_ids=[uuid4()],
    )
    values.update(kw)
    return values


def _financial(item_id: str = "i2", **kw) -> dict:
    values = dict(
        item_id=item_id,
        analysis_type="financial",
        calculation_ids=[uuid4()],
        additional_evidence_ids=[],
    )
    values.update(kw)
    return values


def _macro(item_id: str = "i3", **kw) -> dict:
    values = dict(
        item_id=item_id,
        analysis_type="macro",
        macro_driver_evidence_ids=[uuid4()],
        company_evidence_ids=[uuid4()],
    )
    values.update(kw)
    return values


def _valuation(item_id: str = "i4", **kw) -> dict:
    values = dict(
        item_id=item_id,
        analysis_type="valuation",
        comparison_ids=[uuid4()],
    )
    values.update(kw)
    return values


def _request(items: list[dict], **kw) -> dict:
    values = dict(
        company_id=uuid4(),
        research_question=_QUESTION,
        analysis_as_of=_CUTOFF,
        analysis_work_items=items,
    )
    values.update(kw)
    return values


# ---------------------------------------------------------------- union


def test_each_analysis_type_parses() -> None:
    items = [
        _generic(item_id="a", analysis_type="business"),
        _generic(item_id="b", analysis_type="event"),
        _generic(item_id="c", analysis_type="risk"),
        _financial(item_id="d"),
        _macro(item_id="e"),
        _valuation(item_id="f"),
    ]
    request = Stage4WorkflowRequest(**_request(items))
    assert [item.item_id for item in request.analysis_work_items] == ["a", "b", "c", "d", "e", "f"]
    assert [item.analysis_type for item in request.analysis_work_items] == [
        "business", "event", "risk", "financial", "macro", "valuation",
    ]


def test_unknown_analysis_type_rejected() -> None:
    with pytest.raises(ValidationError):
        Stage4WorkflowRequest(**_request([_generic(analysis_type="quant")]))


def test_work_item_field_mismatch_rejected() -> None:
    # financial item 缺 calculation_ids / 混入 evidence_card_ids → 拒绝。
    with pytest.raises(ValidationError):
        Stage4WorkflowRequest(
            **_request([dict(item_id="i1", analysis_type="financial", evidence_card_ids=[uuid4()])])
        )


def test_macro_pool_overlap_rejected() -> None:
    shared = uuid4()
    with pytest.raises(ValidationError):
        Stage4WorkflowRequest(
            **_request(
                [
                    _macro(
                        macro_driver_evidence_ids=[shared],
                        company_evidence_ids=[shared],
                    )
                ]
            )
        )


def test_empty_evidence_ids_rejected() -> None:
    with pytest.raises(ValidationError):
        Stage4WorkflowRequest(**_request([_generic(evidence_card_ids=[])]))


# ---------------------------------------------------------------- boundaries


def test_zero_items_rejected() -> None:
    with pytest.raises(ValidationError):
        Stage4WorkflowRequest(**_request([]))


def test_too_many_items_rejected() -> None:
    items = [_generic(item_id=f"i{n}") for n in range(MAX_ANALYSIS_WORK_ITEMS + 1)]
    with pytest.raises(ValidationError):
        Stage4WorkflowRequest(**_request(items))


def test_duplicate_item_id_rejected() -> None:
    with pytest.raises(ValidationError):
        Stage4WorkflowRequest(**_request([_generic(item_id="dup"), _financial(item_id="dup")]))


def test_min_max_constants_consistent() -> None:
    assert MIN_ANALYSIS_WORK_ITEMS == 1
    assert MAX_ANALYSIS_WORK_ITEMS == 12


def test_item_id_and_question_trimmed() -> None:
    request = Stage4WorkflowRequest(
        **_request([_generic(item_id="  i1  ")], research_question=f"  {_QUESTION}  ")
    )
    assert request.analysis_work_items[0].item_id == "i1"
    assert request.research_question == _QUESTION


# ---------------------------------------------------------------- JSON-safe


def test_dump_json_is_checkpoint_safe() -> None:
    request = Stage4WorkflowRequest(**_request([_generic(), _financial(), _macro(), _valuation()]))
    dump = [item.model_dump(mode="json") for item in request.analysis_work_items]
    blob = json.dumps({"company_id": str(request.company_id), "items": dump})
    assert isinstance(blob, str)
    # 只放 IDs：dump 不含 Evidence text / Calculation blob / prompt 类字段。
    text_blob = json.dumps(dump)
    for forbidden in ("evidence_statement", "result_value", "prompt", "reasoning_content"):
        assert forbidden not in text_blob
    for item in dump:
        assert isinstance(item["item_id"], str)
        assert isinstance(item["analysis_type"], str)
        assert all(isinstance(c, str) for c in item.get("evidence_card_ids", []) or [])
