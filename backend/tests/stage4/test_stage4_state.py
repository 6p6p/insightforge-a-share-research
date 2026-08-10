"""Stage4WorkflowState tests (spec G + spec Q): reducer order independence + JSON-safe.

覆盖：
- `merge_analysis_results` 按 (item_id, analysis_type) 去重；合并顺序无关；
- retry/resume 幂等：同 item 重放不产生重复结果；
- state 全字段 checkpoint-safe（json.dumps 可序列化）。
"""

import json

from app.stage4.state import Stage4WorkflowState, merge_analysis_results


def _result(item_id: str, analysis_type: str, *claim_ids: str) -> dict:
    return {"item_id": item_id, "analysis_type": analysis_type, "claim_ids": list(claim_ids)}


def test_reducer_dedupes_and_merges() -> None:
    merged = merge_analysis_results(
        [_result("a", "business", "c1", "c2")],
        [_result("a", "business", "c1", "c2")],  # 同 item 重放 → 去重
    )
    assert merged == [_result("a", "business", "c1", "c2")]


def test_reducer_is_order_independent() -> None:
    first = [_result("a", "business", "c1"), _result("b", "financial", "c2")]
    second = [_result("b", "financial", "c2"), _result("a", "business", "c1")]
    ab = merge_analysis_results([], first)
    ba = merge_analysis_results([], second)
    # 集合一致；顺序无关（最终由 collect canonical sort 决定 claim_ids）。
    assert {(r["item_id"], r["analysis_type"]) for r in ab} == {
        (r["item_id"], r["analysis_type"]) for r in ba
    }
    assert {c for r in ab for c in r["claim_ids"]} == {c for r in ba for c in r["claim_ids"]}


def test_reducer_accumulates_across_calls() -> None:
    current = None
    for batch in (
        [_result("a", "business", "c1")],
        [_result("b", "macro", "c2")],
        [_result("a", "business", "c1")],  # 重放
    ):
        current = merge_analysis_results(current, batch)
    assert len(current) == 2


def test_state_is_json_safe() -> None:
    state: Stage4WorkflowState = {
        "company_id": "00000000-0000-0000-0000-000000000001",
        "research_question": "q",
        "analysis_as_of": "2026-08-10",
        "analysis_work_items": [
            {
                "item_id": "i1",
                "analysis_type": "business",
                "evidence_card_ids": ["00000000-0000-0000-0000-000000000002"],
            }
        ],
        "analysis_results": [_result("i1", "business", "c1")],
        "claim_ids": ["c1"],
        "synthesis_id": None,
        "synthesis_result_id": None,
    }
    blob = json.dumps(state)
    assert isinstance(blob, str)
    assert "reasoning_content" not in blob
