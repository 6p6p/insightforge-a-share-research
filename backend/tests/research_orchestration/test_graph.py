"""Top-level research orchestration graph topology unit tests（spec I/L/M，0 DB）。

节点 factory 惰性捕获 deps（build 时不 dereference）→ `dependencies=None` 也能
编译。真实执行拓扑（happy / fulfill / waiting_manual / stage4 child / stage5
routes / continuation）由集成测试 Cases 覆盖；这里做结构烟测：节点集合精确 +
conditional 路由函数判定。
"""

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.research_orchestration.graph import (
    build_top_level_research_orchestration_graph,
)
from app.research_orchestration.nodes import (
    route_after_collect_synthesis,
    route_backflow_progress,
    route_readiness,
    route_readiness_after_fulfill,
    route_stage5_result,
)

# 期望节点集合（graph.py topology 注释中列出的 21 个 node，7A.2B.2 spec L +
# 7A.2B.3 backflow loop）。
_EXPECTED_NODES = {
    "ensure_plan",
    "ensure_route",
    "prepare",
    "fulfill",
    "prepare_again",
    "ensure_stage4_child",
    "run_or_resume_stage4",
    "collect_synthesis",
    "ensure_stage5_child",
    "run_or_resume_stage5",
    "awaiting_stage5",
    "complete_orchestration",
    "stage5_failed",
    "stage5_cancelled",
    "waiting_manual",
    "plan_supplemental_research",
    "execute_supplemental_research",
    "verify_progress",
    "prepare_updated_analysis",
    "fulfill_request",
    "research_backflow_manual",
}

# 编译后 graph.nodes 还会带 __start__ / __end__ 哨兵节点。
_SENTINELS = {"__start__", "__end__"}


def _node_names(graph) -> set[str]:
    return set(graph.nodes) - _SENTINELS


def test_graph_builds_with_expected_nodes() -> None:
    graph = build_top_level_research_orchestration_graph(None, InMemorySaver())
    assert _node_names(graph) == _EXPECTED_NODES


def test_graph_rebuilds_idempotently() -> None:
    g1 = build_top_level_research_orchestration_graph(None, InMemorySaver())
    g2 = build_top_level_research_orchestration_graph(None, InMemorySaver())
    assert _node_names(g1) == _node_names(g2) == _EXPECTED_NODES


def test_route_readiness_ready() -> None:
    assert route_readiness({"preparation_ready": True}) == "ready"
    assert route_readiness({"preparation_ready": False}) == "not_ready"
    assert route_readiness({}) == "not_ready"  # 缺失 key → 保守 not ready


def test_route_readiness_after_fulfill() -> None:
    assert route_readiness_after_fulfill({"preparation_ready": True}) == "ready"
    assert route_readiness_after_fulfill({"preparation_ready": False}) == "waiting_manual"
    assert route_readiness_after_fulfill({}) == "waiting_manual"


# ------------------------------------------------------------------ route_stage5_result


@pytest.mark.parametrize(
    "status",
    ["completed", "waiting_human", "research_required", "failed", "cancelled"],
)
def test_route_stage5_result_known_statuses(status: str) -> None:
    # research_required + round 缺失（0）< MAX → 保持 research_required（进 backflow loop）。
    assert route_stage5_result({"stage5_run_status": status}) == status


def test_route_stage5_result_research_required_below_limit_enters_backflow() -> None:
    """research_required + round < MAX_BACKFLOW_RESEARCH_ROUNDS → 进入 backflow loop。"""
    assert (
        route_stage5_result({"stage5_run_status": "research_required", "backflow_round": 0})
        == "research_required"
    )
    assert (
        route_stage5_result({"stage5_run_status": "research_required", "backflow_round": 1})
        == "research_required"
    )


def test_route_stage5_result_research_required_limit_reached() -> None:
    """research_required + round >= MAX → research_backflow_manual（limit）。"""
    assert (
        route_stage5_result({"stage5_run_status": "research_required", "backflow_round": 2})
        == "research_backflow_manual"
    )


def test_route_stage5_result_unknown_status_raises() -> None:
    """未知 / 缺失 stage5_run_status → ValueError（programming error，不静默路由）。"""
    with pytest.raises(ValueError, match="invalid stage5_run_status"):
        route_stage5_result({"stage5_run_status": "banana"})
    with pytest.raises(ValueError, match="invalid stage5_run_status"):
        route_stage5_result({})


# ------------------------------------------------------------------ backflow 条件路由


def test_route_backflow_progress() -> None:
    assert route_backflow_progress({"backflow_progress": True}) == "progress"
    assert route_backflow_progress({"backflow_progress": False}) == "no_progress"
    assert route_backflow_progress({}) == "no_progress"


def test_route_after_collect_synthesis() -> None:
    assert route_after_collect_synthesis({}) == "ensure_stage5_child"  # 首启
    assert route_after_collect_synthesis({"backflow_round": 0}) == "ensure_stage5_child"
    assert route_after_collect_synthesis({"backflow_round": 1}) == "fulfill_request"
    assert route_after_collect_synthesis({"backflow_round": 2}) == "fulfill_request"
