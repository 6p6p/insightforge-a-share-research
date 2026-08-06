"""LangGraph state schema for InsightForge research workflows."""

from typing import Annotated, TypedDict

from app.workflows.reducers import merge_unique_strings


class InsightForgeState(TypedDict, total=False):
    task_id: str
    run_id: str
    company_query: str
    modules: list[str]
    questions: list[str]
    current_stage: str
    progress: int
    research_plan: dict[str, object]
    completed_nodes: Annotated[list[str], merge_unique_strings]
    simulation_complete: bool
    require_plan_approval: bool
    plan_approved: bool | None
    pending_human_action: dict[str, object] | None
