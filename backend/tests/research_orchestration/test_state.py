"""Research orchestration LangGraph state unit tests（spec H，0 DB）。

顶层 state 只允许 checkpoint-safe 小对象：str / bool / list[str] / str|None。
不放过 report / evidence body / claims / raw / prompt / API key / model。
"""

from app.research_orchestration.state import ResearchOrchestrationState


def test_state_keys_exact() -> None:
    assert set(ResearchOrchestrationState.__annotations__) == {
        "orchestration_id",
        "task_id",
        "research_plan_id",
        "current_phase",
        "preparation_ready",
        "missing_need_codes",
        "stage4_child_run_id",
        "current_child_run_id",
        "synthesis_result_id",
        "stage5_run_status",
        "research_request_id",
        "backflow_round",
        "backflow_plan_id",
        "backflow_new_evidence_card_ids",
        "backflow_manual_reasons",
        "backflow_executor_manual_reasons",
        "backflow_progress",
        "backflow_manual_reason",
        "fulfillment_id",
        "error_code",
    }


def test_state_holds_only_ids_and_phases() -> None:
    # state 只存 ID 与阶段；**不存** synthesis / stage4 request / claim 正文。
    for key in ("orchestration_id", "task_id", "research_plan_id", "current_phase"):
        assert key in ResearchOrchestrationState.__annotations__
    for forbidden in ("synthesis_result", "stage4_request", "claims", "report", "raw"):
        assert forbidden not in ResearchOrchestrationState.__annotations__


def test_state_value_types_checkpoint_safe() -> None:
    # 全部值类型都是 checkpoint-safe 小对象。
    types = ResearchOrchestrationState.__annotations__
    assert types["preparation_ready"] is bool
    assert types["missing_need_codes"] == list[str]  # GenericAlias，不内联 → 用 ==
    for key in (
        "orchestration_id",
        "task_id",
        "research_plan_id",
        "current_phase",
        "stage4_child_run_id",
        "current_child_run_id",
        "synthesis_result_id",
        "stage5_run_status",
        "research_request_id",
        "error_code",
    ):
        assert types[key] is str
