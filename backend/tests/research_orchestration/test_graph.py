"""Top-level research orchestration graph topology unit tests（spec I，0 DB）。

节点 factory 惰性捕获 deps（build 时不 dereference）→ `dependencies=None` 也能
编译。真实执行拓扑（happy / fulfill / waiting_manual / stage4 child）由集成测试
Cases 1-4 覆盖；这里做结构烟测：节点集合精确 + conditional 路由函数判定。
"""

from langgraph.checkpoint.memory import InMemorySaver

from app.research_orchestration.graph import (
    build_top_level_research_orchestration_graph,
)
from app.research_orchestration.nodes import (
    route_readiness,
    route_readiness_after_fulfill,
)

# 期望节点集合（graph.py topology 注释中列出的 10 个 node）。
_EXPECTED_NODES = {
    "ensure_plan",
    "ensure_route",
    "prepare",
    "fulfill",
    "prepare_again",
    "ensure_stage4_child",
    "run_or_resume_stage4",
    "collect_synthesis",
    "awaiting_stage5",
    "waiting_manual",
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
