"""Stage 5 report control workflow graph (spec D/O): topology + builder.

Topology:
    START → validate_stage5_request → build_report_draft → assemble_report
        → check_report → audit_report → route_action
        ├─ terminal（finalize / research_required / revision_limit_exceeded）→ END
        ├─ rewrite → rewrite_sections → assemble_report（bounded loop，spec O）
        └─ human_review → wait_human（interrupt）→
              ├─ approve → finalize_on_approve → END（spec R：Check 须 pass）
              ├─ rewrite → rewrite_sections → assemble_report
              └─ research / cancel → END（terminal research_required / cancelled）

- 修订环：rewrite 后**新** Report（spec N）→ 新 Check → 新 Audit → 新 route；
  `route_action` 超限（revision_round > MAX_STAGE5_REVISION_ROUNDS）→ terminal
  `revision_limit_exceeded`（WorkflowRun FAILED）；
- 人审：真实 `interrupt()`（spec Q）；`build_stage5_report_graph` 集中注入依赖，
  自动测试全部用 Fake models；不在这里重新初始化 model factory。
"""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from app.stage5.dependencies import Stage5WorkflowDependencies
from app.stage5.nodes import (
    make_assemble_report_node,
    make_audit_report_node,
    make_build_report_draft_node,
    make_check_report_node,
    make_finalize_on_approve_node,
    make_rewrite_sections_node,
    make_route_action_node,
    make_validate_stage5_request_node,
    make_wait_human_node,
    route_after_action,
    route_after_human,
)
from app.stage5.state import Stage5WorkflowState


def build_stage5_report_graph(
    dependencies: Stage5WorkflowDependencies,
    checkpointer: BaseCheckpointSaver | None = None,
):
    """编译 Stage 5 报告控制流 graph。

    - `dependencies`：现有 Outline / DraftSection / Report / Check / Audit /
      Review / Revision Services（DI）；
    - `checkpointer`：LangGraph checkpointer（None → 内存，单元测试用）。
    """
    builder = StateGraph(Stage5WorkflowState)
    builder.add_node("validate_stage5_request", make_validate_stage5_request_node())
    builder.add_node("build_report_draft", make_build_report_draft_node(dependencies))
    builder.add_node("assemble_report", make_assemble_report_node(dependencies))
    builder.add_node("check_report", make_check_report_node(dependencies))
    builder.add_node("audit_report", make_audit_report_node(dependencies))
    builder.add_node("route_action", make_route_action_node(dependencies))
    builder.add_node("rewrite_sections", make_rewrite_sections_node(dependencies))
    builder.add_node("wait_human", make_wait_human_node())
    builder.add_node("finalize_on_approve", make_finalize_on_approve_node(dependencies))

    builder.add_edge(START, "validate_stage5_request")
    builder.add_edge("validate_stage5_request", "build_report_draft")
    builder.add_edge("build_report_draft", "assemble_report")
    builder.add_edge("assemble_report", "check_report")
    builder.add_edge("check_report", "audit_report")
    builder.add_edge("audit_report", "route_action")
    builder.add_conditional_edges(
        "route_action",
        route_after_action,
        {
            "rewrite_sections": "rewrite_sections",
            "wait_human": "wait_human",
            "END": END,
        },
    )
    # 修订环：rewrite 后回 assemble_report 装配新 Report（spec N bounded loop）。
    builder.add_edge("rewrite_sections", "assemble_report")
    builder.add_conditional_edges(
        "wait_human",
        route_after_human,
        {
            "rewrite_sections": "rewrite_sections",
            "finalize_on_approve": "finalize_on_approve",
            "END": END,
        },
    )
    builder.add_edge("finalize_on_approve", END)
    return builder.compile(checkpointer=checkpointer)
