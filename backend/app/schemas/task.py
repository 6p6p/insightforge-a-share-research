"""Pydantic contracts for research task creation and queries."""

from datetime import date, datetime, timedelta
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.tasks import ResearchModule, TaskStage, TaskStatus
from app.services.task_status_projection import PUBLIC_STATUS_NOT_STARTED

_MAX_QUESTIONS = 20
_MAX_QUESTION_LENGTH = 500
# AUTO 默认窗口：近 3 年至今（与前端 TaskCreateForm 默认值一致）。
_DEFAULT_WINDOW_DAYS = 3 * 365
# AUTO 默认模块：全部研究模块（与前端 DEFAULT_MODULES 一致）。
_DEFAULT_MODULES = list(ResearchModule)


class TaskCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    # 产品最小输入 = 公司名称/代码；研究周期与模块都有 AUTO 默认值（可覆盖）。
    company_query: str = Field(min_length=1, max_length=100)
    research_start_date: date = Field(
        default_factory=lambda: date.today() - timedelta(days=_DEFAULT_WINDOW_DAYS)
    )
    research_end_date: date = Field(default_factory=date.today)
    modules: list[ResearchModule] = Field(
        default_factory=lambda: list(_DEFAULT_MODULES), min_length=1
    )
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
    # canonical public projection（未开始/进行中/等待确认/已完成/失败）：
    # 由 task + 最新 orchestration 推导（app.services.task_status_projection），
    # 所有前端位置统一读取，不再各自推导。
    public_status: str = PUBLIC_STATUS_NOT_STARTED
    created_at: datetime
    updated_at: datetime


class TaskListResponse(BaseModel):
    items: list[TaskResponse]
    total: int
    limit: int
    offset: int
