"""Deterministic research workflow graph."""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from app.workflows.nodes.simulation import (
    build_research_plan,
    finish_simulation,
    load_task_context,
    request_plan_approval,
)
from app.workflows.state import InsightForgeState

GRAPH_NAME = "research_workflow_simulation"
GRAPH_VERSION = "1d.1"


def build_research_workflow(checkpointer: BaseCheckpointSaver):
    builder = StateGraph(InsightForgeState)
    builder.add_node("load_task_context", load_task_context)
    builder.add_node("build_research_plan", build_research_plan)
    builder.add_node("request_plan_approval", request_plan_approval)
    builder.add_node("finish_simulation", finish_simulation)
    builder.add_edge(START, "load_task_context")
    builder.add_edge("load_task_context", "build_research_plan")
    builder.add_edge("build_research_plan", "request_plan_approval")
    builder.add_edge("request_plan_approval", "finish_simulation")
    builder.add_edge("finish_simulation", END)
    return builder.compile(checkpointer=checkpointer)
