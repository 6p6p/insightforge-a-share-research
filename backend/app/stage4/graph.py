"""Stage 4 analysis workflow graph (spec J): topology + builder.

Topology:
    START → validate_analysis_plan → dispatch_parallel_analysis
        → (Send × N) run_analysis_item → collect_claim_ids → synthesize_claims → END

- 使用真实 Send dynamic fan-out（conditional-edge 函数返回 Send list）；
- 并发 worker 完成顺序不影响最终 claim_ids（collect canonical sort + dedupe）；
- `build_stage4_analysis_graph(dependencies)`：依赖集中注入；自动测试全部用
  Fake models；不在这里重新初始化 model factory。
"""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from app.stage4.dependencies import Stage4AnalysisDependencies
from app.stage4.nodes import (
    fan_out_workers,
    make_collect_claim_ids_node,
    make_dispatch_parallel_analysis_node,
    make_run_analysis_item_node,
    make_synthesize_claims_node,
    make_validate_analysis_plan_node,
)
from app.stage4.state import Stage4WorkflowState

# 持久化到 workflow_runs.graph_name / graph_version（spec F：复用既有字段）。
STAGE4_GRAPH_NAME = "stage4_analysis"
STAGE4_GRAPH_VERSION = "1"


def build_stage4_analysis_graph(
    dependencies: Stage4AnalysisDependencies,
    checkpointer: BaseCheckpointSaver | None = None,
):
    """编译 Stage 4 分析工作流 graph。

    - `dependencies`：现有 Analysis / Synthesis Services（DI）；
    - `checkpointer`：LangGraph checkpointer（None → 内存，单元测试用）。
    """
    builder = StateGraph(Stage4WorkflowState)
    builder.add_node("validate_analysis_plan", make_validate_analysis_plan_node())
    builder.add_node("dispatch_parallel_analysis", make_dispatch_parallel_analysis_node())
    builder.add_node("run_analysis_item", make_run_analysis_item_node(dependencies))
    builder.add_node("collect_claim_ids", make_collect_claim_ids_node())
    builder.add_node("synthesize_claims", make_synthesize_claims_node(dependencies))

    builder.add_edge(START, "validate_analysis_plan")
    builder.add_edge("validate_analysis_plan", "dispatch_parallel_analysis")
    builder.add_conditional_edges(
        "dispatch_parallel_analysis",
        fan_out_workers,
        ["run_analysis_item", "collect_claim_ids"],
    )
    # 所有 worker 在同一 super-step 完成后 join 到 collect（collect 恰好跑一次）。
    builder.add_edge("run_analysis_item", "collect_claim_ids")
    builder.add_edge("collect_claim_ids", "synthesize_claims")
    builder.add_edge("synthesize_claims", END)
    return builder.compile(checkpointer=checkpointer)
