"""Tests for the research task domain enums."""

from app.domain.tasks import ResearchModule, TaskStage, TaskStatus


def test_task_status_values_are_stable() -> None:
    assert [status.value for status in TaskStatus] == [
        "pending",
        "running",
        "waiting_human",
        "retrying",
        "completed",
        "failed",
        "cancelled",
    ]


def test_task_stage_values_are_stable() -> None:
    assert [stage.value for stage in TaskStage] == [
        "created",
        "planning",
        "collecting",
        "parsing",
        "evidence_extraction",
        "analyzing",
        "synthesizing",
        "writing",
        "checking",
        "auditing",
        "exporting",
    ]


def test_research_module_values() -> None:
    assert [module.value for module in ResearchModule] == [
        "company_profile",
        "business",
        "financial",
        "events",
        "macro",
        "risk",
    ]


def test_status_and_stage_are_separate() -> None:
    status_values = {status.value for status in TaskStatus}
    stage_values = {stage.value for stage in TaskStage}
    assert "planning" not in status_values
    assert "writing" not in status_values
    assert "pending" not in stage_values
    assert "completed" not in stage_values


def test_enums_are_str_enums() -> None:
    assert isinstance(TaskStatus.PENDING, str)
    assert isinstance(TaskStage.CREATED, str)
    assert isinstance(ResearchModule.RISK, str)
