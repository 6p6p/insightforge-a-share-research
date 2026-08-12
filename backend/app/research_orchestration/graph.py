"""Top-level research orchestration graph (stage 7A.2B.1 spec I + 7A.2B.2 L/M).

Topology:
    START → ensure_plan → ensure_route → prepare
        → (ready: ensure_stage4_child | not_ready: fulfill)
        → fulfill → prepare_again
        → (ready: ensure_stage4_child | waiting_manual: waiting_manual END)
        → ensure_stage4_child → run_or_resume_stage4 → collect_synthesis
        → ensure_stage5_child → run_or_resume_stage5
        → route_stage5_result（**纯 state 路由**）
          · waiting_human → awaiting_stage5 END（pause；spec M continuation）
          · completed → complete_orchestration END
          · research_required → pause_for_research END（spec P）
          · failed → stage5_failed END
          · cancelled → stage5_cancelled END

- 顶层是真实 LangGraph graph（`build_top_level_research_orchestration_graph`），
  PG Checkpointer、`thread_id = orchestration_id`（spec N：顶层线程 != child
  Stage4/Stage5 线程，child 线程仍是 `thread_id = run_id`）；
- 节点全部幂等（plan/route/prepare replay；Stage4/5 child exact get_child 复用，
  spec D/K）；
- `awaiting_stage5` 是 **pause 节点**（Stage5 child WAITING_HUMAN）：graph 到 END
  暂停、status=waiting_human；人工裁决后 runner 用 `aupdate_state(as_node=
  ensure_stage5_child)` 重新进入 `run_or_resume_stage5` 重新判定（spec M）；
- `waiting_manual` 是 fulfill 后仍 not ready 的 terminal（status=waiting_human，
  0 个 WorkflowRun）。
"""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from app.research_orchestration.dependencies import ResearchOrchestrationDependencies
from app.research_orchestration.nodes import (
    make_awaiting_stage5_node,
    make_collect_synthesis_node,
    make_complete_orchestration_node,
    make_ensure_plan_node,
    make_ensure_route_node,
    make_ensure_stage4_child_node,
    make_ensure_stage5_child_node,
    make_fulfill_node,
    make_pause_for_research_node,
    make_prepare_again_node,
    make_prepare_node,
    make_run_or_resume_stage4_node,
    make_run_or_resume_stage5_node,
    make_stage5_cancelled_node,
    make_stage5_failed_node,
    make_waiting_manual_node,
    route_readiness,
    route_readiness_after_fulfill,
    route_stage5_result,
)
from app.research_orchestration.state import ResearchOrchestrationState

# 顶层 graph 标识（本阶段用于 recovery 协调器定位顶层 checkpoint；顶层不在
# workflow_runs，仅作常量保留）。
TOP_LEVEL_GRAPH_NAME = "stage7_top_level"
TOP_LEVEL_GRAPH_VERSION = "1"


def build_top_level_research_orchestration_graph(
    dependencies: ResearchOrchestrationDependencies,
    checkpointer: BaseCheckpointSaver | None = None,
):
    """编译顶层研究编排 graph。

    - `dependencies`：`ResearchOrchestrationDependencies`（复用既有 services，DI）；
    - `checkpointer`：LangGraph checkpointer（None → 内存，单元测试用）。
    """
    builder = StateGraph(ResearchOrchestrationState)
    builder.add_node("ensure_plan", make_ensure_plan_node(dependencies))
    builder.add_node("ensure_route", make_ensure_route_node(dependencies))
    builder.add_node("prepare", make_prepare_node(dependencies))
    builder.add_node("fulfill", make_fulfill_node(dependencies))
    builder.add_node("prepare_again", make_prepare_again_node(dependencies))
    builder.add_node("ensure_stage4_child", make_ensure_stage4_child_node(dependencies))
    builder.add_node("run_or_resume_stage4", make_run_or_resume_stage4_node(dependencies))
    builder.add_node("collect_synthesis", make_collect_synthesis_node(dependencies))
    builder.add_node("ensure_stage5_child", make_ensure_stage5_child_node(dependencies))
    builder.add_node("run_or_resume_stage5", make_run_or_resume_stage5_node(dependencies))
    builder.add_node("awaiting_stage5", make_awaiting_stage5_node(dependencies))
    builder.add_node("complete_orchestration", make_complete_orchestration_node(dependencies))
    builder.add_node("pause_for_research", make_pause_for_research_node(dependencies))
    builder.add_node("stage5_failed", make_stage5_failed_node(dependencies))
    builder.add_node("stage5_cancelled", make_stage5_cancelled_node(dependencies))
    builder.add_node("waiting_manual", make_waiting_manual_node(dependencies))

    builder.add_edge(START, "ensure_plan")
    builder.add_edge("ensure_plan", "ensure_route")
    builder.add_edge("ensure_route", "prepare")
    builder.add_conditional_edges(
        "prepare",
        route_readiness,
        {"ready": "ensure_stage4_child", "not_ready": "fulfill"},
    )
    builder.add_edge("fulfill", "prepare_again")
    builder.add_conditional_edges(
        "prepare_again",
        route_readiness_after_fulfill,
        {"ready": "ensure_stage4_child", "waiting_manual": "waiting_manual"},
    )
    builder.add_edge("ensure_stage4_child", "run_or_resume_stage4")
    builder.add_edge("run_or_resume_stage4", "collect_synthesis")
    builder.add_edge("collect_synthesis", "ensure_stage5_child")
    builder.add_edge("ensure_stage5_child", "run_or_resume_stage5")
    builder.add_conditional_edges(
        "run_or_resume_stage5",
        route_stage5_result,
        {
            "completed": "complete_orchestration",
            "waiting_human": "awaiting_stage5",
            "research_required": "pause_for_research",
            "failed": "stage5_failed",
            "cancelled": "stage5_cancelled",
        },
    )
    builder.add_edge("awaiting_stage5", END)
    builder.add_edge("complete_orchestration", END)
    builder.add_edge("pause_for_research", END)
    builder.add_edge("stage5_failed", END)
    builder.add_edge("stage5_cancelled", END)
    builder.add_edge("waiting_manual", END)
    return builder.compile(checkpointer=checkpointer)
