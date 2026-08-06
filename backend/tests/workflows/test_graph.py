"""Tests for the simulation workflow graph using an in-memory checkpointer."""

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

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


async def test_graph_approval_interrupts() -> None:
    graph = build_research_workflow(InMemorySaver())
    config = {"configurable": {"thread_id": "approve-1"}}
    state = {**_INITIAL_STATE, "require_plan_approval": True}
    chunks = [chunk async for chunk in graph.astream(state, config, stream_mode="updates")]
    assert any("__interrupt__" in chunk for chunk in chunks)
    snapshot = await graph.aget_state(config)
    assert any(task.interrupts for task in snapshot.tasks)


async def test_graph_resume_after_approval() -> None:
    graph = build_research_workflow(InMemorySaver())
    config = {"configurable": {"thread_id": "approve-2"}}
    state = {**_INITIAL_STATE, "require_plan_approval": True}
    async for _ in graph.astream(state, config, stream_mode="updates"):
        pass
    async for _ in graph.astream(
        Command(resume={"action_type": "approve_plan"}), config, stream_mode="updates"
    ):
        pass
    final = await graph.aget_state(config)
    assert final.values["plan_approved"] is True
    assert final.values["simulation_complete"] is True


async def test_graph_invalid_resume_rejected() -> None:
    graph = build_research_workflow(InMemorySaver())
    config = {"configurable": {"thread_id": "approve-3"}}
    state = {**_INITIAL_STATE, "require_plan_approval": True}
    async for _ in graph.astream(state, config, stream_mode="updates"):
        pass
    with pytest.raises(ValueError):
        async for _ in graph.astream(
            Command(resume={"action_type": "reject_plan"}), config, stream_mode="updates"
        ):
            pass


async def test_graph_without_approval_completes() -> None:
    graph = build_research_workflow(InMemorySaver())
    result = await graph.ainvoke(_INITIAL_STATE, {"configurable": {"thread_id": "no-appr"}})
    assert result["plan_approved"] is True
    assert result["simulation_complete"] is True
