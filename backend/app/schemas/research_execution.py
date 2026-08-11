"""Pydantic contracts for Stage 6A web workbench research execution.

- `ResearchExecutionRequest`：Web 启动真实研究的输入边界。**只携带显式
  Stage 4 work plan**（analysis_work_items，复用 stage4.contracts 的
  discriminated union）；`research_question` / `analysis_as_of` 由
  ResearchExecutionService 从 ResearchTask 派生（questions[0] /
  research_end_date）——**不假装自动 source planning 已完成**（Stage 2
  planner 尚未接入 Web）。
- `TaskWorkspaceResponse`：task workspace projection（spec E）。
- `ArtifactSummary`：公司级证据链产物计数（source / evidence / claim /
  report / review issue）。
"""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.company import CompanyIdentityResponse
from app.schemas.task import TaskResponse
from app.schemas.workflow import WorkflowRunResponse
from app.stage4.contracts import (
    MAX_ANALYSIS_WORK_ITEMS,
    MIN_ANALYSIS_WORK_ITEMS,
    AnalysisWorkItem,
)


class ResearchExecutionRequest(BaseModel):
    """Web 启动真实研究的显式 work plan。

    字段显式、可审计：analysis_work_items 直接来自前端表单（每条引用真实
    evidence / calculation / comparison ID）。研究问题与分析基准日不在此处
    重复收集——它们属于 ResearchTask（Stage 6A 以任务为准）。
    """

    model_config = ConfigDict(frozen=True)

    analysis_work_items: list[AnalysisWorkItem]

    @model_validator(mode="after")
    def _validate_work_items(self) -> "ResearchExecutionRequest":
        count = len(self.analysis_work_items)
        if not (MIN_ANALYSIS_WORK_ITEMS <= count <= MAX_ANALYSIS_WORK_ITEMS):
            raise ValueError(
                f"analysis_work_items 必须在 {MIN_ANALYSIS_WORK_ITEMS}.."
                f"{MAX_ANALYSIS_WORK_ITEMS} 条"
            )
        item_ids = [item.item_id for item in self.analysis_work_items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("analysis_work_items 的 item_id 必须唯一")
        return self


class ArtifactSummary(BaseModel):
    """公司级证据链产物计数（task workspace projection）。"""

    source_count: int = 0
    evidence_count: int = 0
    claim_count: int = 0
    report_count: int = 0
    review_issue_count: int = 0


class TaskWorkspaceResponse(BaseModel):
    """Task workspace projection（spec E）：task + 解析公司 + 当前 run + 产物计数。"""

    task: TaskResponse
    resolved_company: CompanyIdentityResponse | None = None
    current_run: WorkflowRunResponse | None = None
    artifact_summary: ArtifactSummary = Field(default_factory=ArtifactSummary)
