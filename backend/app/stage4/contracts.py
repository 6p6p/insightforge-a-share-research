"""Stage 4 workflow request contracts (spec G): request + work item union.

`Stage4WorkflowRequest` 是 Stage 4 分析工作流的 API 输入边界：
- company_id / research_question / analysis_as_of + analysis_work_items（1..12）；
- work item 是 **discriminated union**（analysis_type 判别）：只放 **IDs**，
  不把 Evidence text / Calculation blobs / Macro pack / Comparison details
  放进 state；
- item_id 唯一；所有 UUID 列表构造时校验（非空 / 允许为空项除外），服务层仍
  会从真实 PG 二次校验归属。

构造即校验（ValidationError → 调用方 400）。`model_dump(mode="json")` 输出
checkpoint-safe 结构：UUID 序列化为 str。
"""

from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.claims.contracts import ClaimAnalysisDomain

# 一个 Stage 4 请求允许的 work item 数量边界（spec G：1..12）。
MIN_ANALYSIS_WORK_ITEMS = 1
MAX_ANALYSIS_WORK_ITEMS = 12

# 6 类 analysis_type（spec G work item 类型）。
_VALID_ANALYSIS_TYPES = frozenset(
    {
        ClaimAnalysisDomain.BUSINESS.value,
        ClaimAnalysisDomain.EVENT.value,
        ClaimAnalysisDomain.RISK.value,
        ClaimAnalysisDomain.FINANCIAL.value,
        ClaimAnalysisDomain.MACRO.value,
        ClaimAnalysisDomain.VALUATION.value,
    }
)


class _WorkItemBase(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_id: str
    analysis_type: str

    @field_validator("item_id", mode="before")
    @classmethod
    def _strip_item_id(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @model_validator(mode="after")
    def _validate_item_id(self) -> "_WorkItemBase":
        if not self.item_id:
            raise ValueError("item_id 不能为空（trim 后）")
        if self.analysis_type not in _VALID_ANALYSIS_TYPES:
            raise ValueError(f"analysis_type 必须是 {sorted(_VALID_ANALYSIS_TYPES)}")
        return self


class GenericWorkItem(_WorkItemBase):
    """business / event / risk：analysis_type + evidence_card_ids（1..30）。"""

    analysis_type: Literal["business", "event", "risk"]
    evidence_card_ids: list[UUID]

    @field_validator("evidence_card_ids", mode="after")
    @classmethod
    def _non_empty(cls, value: list[UUID]) -> list[UUID]:
        if not value:
            raise ValueError("evidence_card_ids 至少 1 条")
        return value


class FinancialWorkItem(_WorkItemBase):
    """financial：calculation_ids（1..20）+ additional_evidence_ids（0..20）。"""

    analysis_type: Literal["financial"]
    calculation_ids: list[UUID]
    additional_evidence_ids: list[UUID] = []

    @field_validator("calculation_ids", mode="after")
    @classmethod
    def _non_empty_calcs(cls, value: list[UUID]) -> list[UUID]:
        if not value:
            raise ValueError("calculation_ids 至少 1 条")
        return value


class MacroWorkItem(_WorkItemBase):
    """macro：macro_driver_evidence_ids（1..20）+ company_evidence_ids（1..30）。

    两池 namespace 严格分离（同一 evidence 不能同时出现）——与
    MacroAnalysisRequest 服务层规则一致。
    """

    analysis_type: Literal["macro"]
    macro_driver_evidence_ids: list[UUID]
    company_evidence_ids: list[UUID]

    @field_validator("macro_driver_evidence_ids", mode="after")
    @classmethod
    def _non_empty_drivers(cls, value: list[UUID]) -> list[UUID]:
        if not value:
            raise ValueError("macro_driver_evidence_ids 至少 1 条")
        return value

    @field_validator("company_evidence_ids", mode="after")
    @classmethod
    def _non_empty_companies(cls, value: list[UUID]) -> list[UUID]:
        if not value:
            raise ValueError("company_evidence_ids 至少 1 条")
        return value

    @model_validator(mode="after")
    def _validate_pools(self) -> "MacroWorkItem":
        if set(self.macro_driver_evidence_ids) & set(self.company_evidence_ids):
            raise ValueError("macro_driver 池与 company 池不能重叠")
        return self


class ValuationWorkItem(_WorkItemBase):
    """valuation：comparison_ids（1..3）。"""

    analysis_type: Literal["valuation"]
    comparison_ids: list[UUID]

    @field_validator("comparison_ids", mode="after")
    @classmethod
    def _non_empty(cls, value: list[UUID]) -> list[UUID]:
        if not value:
            raise ValueError("comparison_ids 至少 1 条")
        return value


# 判别联合：analysis_type 决定 work item 具体形状。
AnalysisWorkItem = Annotated[
    GenericWorkItem | FinancialWorkItem | MacroWorkItem | ValuationWorkItem,
    Field(discriminator="analysis_type"),
]


class Stage4WorkflowRequest(BaseModel):
    """Stage 4 分析工作流请求（构造时校验，不可变）。

    - task_id：必填 UUID（Stage 4 WorkflowRun 仍必须属于一个 ResearchTask，
      Gate 0 恢复该关系）；创建 run 时真实 PG 校验任务存在；
    - research_question：trim 后非空；
    - analysis_as_of：必填 date（分析基准日，全 item 共享，no-lookahead 边界）；
    - analysis_work_items：1..12，item_id 唯一（去重 + 顺序保留）。
    """

    model_config = ConfigDict(frozen=True)

    task_id: UUID
    company_id: UUID
    research_question: str
    analysis_as_of: date
    analysis_work_items: list[AnalysisWorkItem]

    @field_validator("research_question", mode="before")
    @classmethod
    def _strip_question(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @model_validator(mode="after")
    def _validate_request(self) -> "Stage4WorkflowRequest":
        if not self.research_question:
            raise ValueError("research_question 不能为空（trim 后）")
        if not (
            MIN_ANALYSIS_WORK_ITEMS <= len(self.analysis_work_items) <= MAX_ANALYSIS_WORK_ITEMS
        ):
            raise ValueError(
                f"analysis_work_items 必须在 {MIN_ANALYSIS_WORK_ITEMS}.."
                f"{MAX_ANALYSIS_WORK_ITEMS} 条"
            )
        ids = [item.item_id for item in self.analysis_work_items]
        if len(ids) != len(set(ids)):
            raise ValueError("analysis_work_items 的 item_id 必须唯一")
        return self
