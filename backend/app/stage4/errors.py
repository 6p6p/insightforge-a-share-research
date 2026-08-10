"""Stable error taxonomy for the Stage 4 analysis workflow (spec 4D.2).

错误消息不包含：evidence 正文、完整 raw content、prompt、UUID 集合明细。
`code` 是稳定错误码。worker 内部各分析服务（Claim / Financial / Macro /
Valuation / Synthesis）抛出的域错误原样向上传播，本模块只定义 graph
编排层的协调错误。
"""


class Stage4WorkflowError(Exception):
    """Stage 4 工作流顶层错误基类。"""

    code = "stage4_workflow_error"
    message = "stage 4 workflow error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message if message is not None else self.message)


class Stage4InvalidPlan(Stage4WorkflowError):
    """analysis_work_items 计划不合法（1..12、item_id 唯一、字段齐全）。"""

    code = "stage4_invalid_plan"
    message = "invalid stage 4 analysis plan"


class Stage4UnknownWorkItemType(Stage4WorkflowError):
    """worker dispatch 遇到未知 analysis_type（请求构造已校验，防御性兜底）。"""

    code = "stage4_unknown_work_item_type"
    message = "unknown stage 4 work item type"


class Stage4InsufficientClaims(Stage4WorkflowError):
    """去重后 Claim 少于 2 条，无法综合（spec R：<2 final claims 稳定失败）。"""

    code = "stage4_insufficient_claims"
    message = "stage 4 synthesis requires at least 2 claims"
