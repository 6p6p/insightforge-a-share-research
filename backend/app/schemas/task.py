"""Pydantic contracts for research task creation and queries."""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.tasks import ResearchModule, TaskStage, TaskStatus

_MAX_QUESTIONS = 20
_MAX_QUESTION_LENGTH = 500


class TaskCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    company_query: str = Field(min_length=1, max_length=100)
    research_start_date: date
    research_end_date: date
    modules: list[ResearchModule] = Field(min_length=1)
    questions: list[str] = Field(default_factory=list, max_length=_MAX_QUESTIONS)
    include_relative_valuation: bool = False
    require_plan_approval: bool = True

    @field_validator("modules")
    @classmethod
    def _dedupe_modules(cls, value: list[ResearchModule]) -> list[ResearchModule]:
        seen: list[ResearchModule] = []
        for module in value:
            if module not in seen:
                seen.append(module)
        return seen

    @field_validator("questions")
    @classmethod
    def _normalize_questions(cls, value: list[str]) -> list[str]:
        seen: list[str] = []
        for question in value:
            stripped = question.strip()
            if not stripped:
                raise ValueError("questions must not contain blank entries")
            if len(stripped) > _MAX_QUESTION_LENGTH:
                raise ValueError("question exceeds maximum length")
            if stripped not in seen:
                seen.append(stripped)
        return seen

    @model_validator(mode="after")
    def _check_date_order(self) -> "TaskCreateRequest":
        if self.research_end_date < self.research_start_date:
            raise ValueError("research_end_date must not be earlier than research_start_date")
        return self


class TaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_id: UUID
    company_query: str
    research_start_date: date
    research_end_date: date
    modules: list[ResearchModule]
    questions: list[str]
    include_relative_valuation: bool
    require_plan_approval: bool
    status: TaskStatus
    current_stage: TaskStage
    progress: int
    created_at: datetime
    updated_at: datetime


class TaskListResponse(BaseModel):
    items: list[TaskResponse]
    total: int
    limit: int
    offset: int
