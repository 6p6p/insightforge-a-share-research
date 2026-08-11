"""Stage 5 report control workflow contracts (spec 5E.2A D/O).

请求 + 常量 + terminal 语义。角色边界（Stage 5 是**确定性编排**的控制环，
spec D/O）：
- graph 只负责编排既有 Services：ReportOutline → DraftSection → Report →
  Check → Audit → ReviewAction →（rewrite → 新 Report → 新 Check → 新 Audit
  bounded loop）／human_review → interrupt（WAITING_HUMAN）／finalize /
  research_required（terminal）；
- 本轮 research 不执行：route=research 直接 terminal `research_required`
  （spec S，不得假装 research completed）；
- rewrite 后必须构造**新** Report（spec N），不 UPDATE 旧 Report；
- `MAX_STAGE5_REVISION_ROUNDS = 2`（spec O）：同一次 Stage 5 执行最多 2 轮
  修订；route 仍为 rewrite 且超限 → terminal `revision_limit_exceeded`、
  WorkflowRun FAILED。

冻结常量：
- `STAGE5_GRAPH_NAME = "stage5_report"` / `STAGE5_GRAPH_VERSION = "1"`
  （persisted workflow_runs.graph_name / graph_version，spec F 复用既有字段）；
- terminal 值：`finalize` / `research_required` / `revision_limit_exceeded` /
  `cancelled`（run 终态投影；run status 由 runner 映射）。
"""

from datetime import date
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

# 一次 Stage 5 执行允许的最大修订轮数（spec O，bounded Stage5 review loop）。
MAX_STAGE5_REVISION_ROUNDS = 2

# 持久化到 workflow_runs.graph_name / graph_version（spec F：复用既有字段）。
STAGE5_GRAPH_NAME = "stage5_report"
STAGE5_GRAPH_VERSION = "1"

# terminal 值（run 终态投影；run status 由 runner 映射）。
STAGE5_TERMINAL_FINALIZE = "finalize"
STAGE5_TERMINAL_RESEARCH_REQUIRED = "research_required"
STAGE5_TERMINAL_REVISION_LIMIT_EXCEEDED = "revision_limit_exceeded"
STAGE5_TERMINAL_CANCELLED = "cancelled"

STAGE5_TERMINAL_VALUES = (
    STAGE5_TERMINAL_FINALIZE,
    STAGE5_TERMINAL_RESEARCH_REQUIRED,
    STAGE5_TERMINAL_REVISION_LIMIT_EXCEEDED,
    STAGE5_TERMINAL_CANCELLED,
)


class Stage5WorkflowRequest(BaseModel):
    """Stage 5 报告控制流请求（构造时校验，不可变）。

    - task_id：必填 UUID（Stage 5 WorkflowRun 仍必须属于一个 ResearchTask）；
      创建 run 时真实 PG 校验任务存在；
    - company_id / research_question / analysis_as_of：Report 派生所需的上下文
      （与 Outline / Report 指纹一致）；
    - synthesis_result_id：必填 UUID（Stage 4 SynthesisResult → Outline 起点，
      spec D：Report 建立在 Stage 4 的综合结论之上）。
    """

    model_config = ConfigDict(frozen=True)

    task_id: UUID
    company_id: UUID
    research_question: str
    analysis_as_of: date
    synthesis_result_id: UUID

    @field_validator("research_question", mode="before")
    @classmethod
    def _strip_question(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @model_validator(mode="after")
    def _validate_request(self) -> "Stage5WorkflowRequest":
        if not self.research_question:
            raise ValueError("research_question 不能为空（trim 后）")
        return self
