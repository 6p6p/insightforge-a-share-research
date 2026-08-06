"""Deterministic simulation nodes; no LLM, network or database access."""

from app.workflows.state import InsightForgeState

_BASE_QUESTIONS_BY_MODULE = {
    "company_profile": "公司主营业务与股权结构如何？",
    "business": "公司各业务板块的收入与毛利结构如何？",
    "financial": "公司近三年收入、利润与现金流表现如何？",
    "events": "公司近期重大事件有哪些？",
    "macro": "公司所处行业与宏观环境如何？",
    "risk": "公司面临的主要风险有哪些？",
}


def load_task_context(state: InsightForgeState) -> dict:
    for field in ("task_id", "company_query", "modules"):
        if not state.get(field):
            raise ValueError(f"missing required field in state: {field}")
    completed = list(state.get("completed_nodes") or [])
    if "load_task_context" not in completed:
        completed.append("load_task_context")
    return {"progress": 20, "completed_nodes": completed}


def build_research_plan(state: InsightForgeState) -> dict:
    modules = list(state.get("modules") or [])
    questions = list(state.get("questions") or [])
    plan_questions = list(questions)
    for module in modules:
        base_question = _BASE_QUESTIONS_BY_MODULE.get(module)
        if base_question is not None and base_question not in plan_questions:
            plan_questions.append(base_question)

    plan = {
        "selected_modules": modules,
        "research_questions": plan_questions,
        "required_source_categories": ["annual_report", "announcement", "news"],
    }
    completed = list(state.get("completed_nodes") or [])
    if "build_research_plan" not in completed:
        completed.append("build_research_plan")
    return {
        "research_plan": plan,
        "current_stage": "planning",
        "progress": 70,
        "completed_nodes": completed,
    }


def finish_simulation(state: InsightForgeState) -> dict:
    completed = list(state.get("completed_nodes") or [])
    if "finish_simulation" not in completed:
        completed.append("finish_simulation")
    return {
        "simulation_complete": True,
        "progress": 100,
        "completed_nodes": completed,
    }
