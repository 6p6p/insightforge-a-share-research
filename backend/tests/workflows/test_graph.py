"""Tests for the simulation workflow graph using an in-memory checkpointer."""

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.workflows.graph import build_research_workflow

_INITIAL_STATE = {
    "task_id": "00000000-0000-0000-0000-000000000001",
    "run_id": "00000000-0000-0000-0000-000000000002",
    "company_query": "600519",
    "modules": ["company_profile", "financial"],
    "questions": [],
    "current_stage": "created",
    "progress": 0,
}

pytestmark = pytest.mark.asyncio


async def test_graph_runs_in_order() -> None:
    graph = build_research_workflow(InMemorySaver())
    result = await graph.ainvoke(_INITIAL_STATE, {"configurable": {"thread_id": "t1"}})

    assert result["simulation_complete"] is True
    assert result["progress"] == 100
    assert result["completed_nodes"] == [
        "load_task_context",
        "build_research_plan",
        "finish_simulation",
    ]
    assert result["current_stage"] == "planning"
    assert result["research_plan"]["selected_modules"] == ["company_profile", "financial"]
    assert len(result["research_plan"]["research_questions"]) == 2
    assert result["research_plan"]["required_source_categories"] == [
        "annual_report",
        "announcement",
        "news",
    ]


async def test_graph_without_thread_id_fails() -> None:
    graph = build_research_workflow(InMemorySaver())
    with pytest.raises(ValueError):
        await graph.ainvoke(_INITIAL_STATE, {})


async def test_graph_does_not_produce_source_fields() -> None:
    graph = build_research_workflow(InMemorySaver())
    result = await graph.ainvoke(_INITIAL_STATE, {"configurable": {"thread_id": "t2"}})
    for key in ("source_records", "evidence", "claims", "report"):
        assert key not in result
