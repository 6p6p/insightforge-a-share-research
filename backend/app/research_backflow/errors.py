"""Research backflow error taxonomy (stage 5E.2B).

错误消息不包含：evidence 正文、完整 raw content、DB URL、UUID 集合明细、raw
provider response、prompt。`code` 是稳定错误码。

integrity / not-found 错误由上游（Review / Report / Synthesis）服务抛出并原样向上
传播，本模块只定义 Backflow 层的协调 / 验证错误。
"""


class ResearchBackflowError(Exception):
    """Research Backflow 域稳定错误基类。"""

    code = "research_backflow_error"
    message = "research backflow error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message if message is not None else self.message)


class ResearchBackflowInputError(ResearchBackflowError):
    """调用方输入不合法（id 类型 / 缺失等）。"""

    code = "research_backflow_input_error"
    message = "invalid research backflow input"


class ResearchBackflowInvalidRun(ResearchBackflowError):
    """source run 不是 stage5 run（graph_name 不符）或不存在。"""

    code = "research_backflow_invalid_run"
    message = "source workflow run is not a stage5 run"


class ResearchBackflowNotResearchTerminal(ResearchBackflowError):
    """source Stage 5 run 的真实 terminal 不是 research_required（spec F）。

    finalize / rewrite / revision_limit_exceeded / cancelled 的 run 不允许创建
    research request——只有 route=research 或 human decision=research 的 run 才能
    产生交接请求。
    """

    code = "research_backflow_not_research_terminal"
    message = "source stage5 run terminal is not research_required"


class ResearchBackflowIllegalTrigger(ResearchBackflowError):
    """legal trigger（spec G）不符：action_type 不是 research（无 human decision）
    或 human_review（有 research decision）。

    finalize / rewrite action、research action 带 human_decision、human_review
    无 research decision——全部拒绝（0 write）。
    """

    code = "research_backflow_illegal_trigger"
    message = "review action does not legally trigger a research request"


class ResearchBackflowInvalidState(ResearchBackflowError):
    """从 run final state 恢复的 IDs 缺失 / 不完整（防御性硬边界）。"""

    code = "research_backflow_invalid_state"
    message = "recovered stage5 final state is incomplete for a research request"


class ResearchBackflowStage5ContextMissing(ResearchBackflowError):
    """service 未绑定 Stage5 checkpoint / deps（只能由 Stage5 runner 注入）。

    直接以 `create_or_get_request(source_stage5_run_id)` 构造时（未经过 runner）
    无法恢复 run final state → 明确拒绝，不静默降级。
    """

    code = "research_backflow_stage5_context_missing"
    message = "research backflow service has no stage5 context bound"


class ResearchBackflowRequestNotFound(ResearchBackflowError):
    """research_request_id 不存在。"""

    code = "research_backflow_request_not_found"
    message = "research backflow request not found"


class ResearchBackflowFulfillmentNotFound(ResearchBackflowError):
    """fulfillment_id 不存在。"""

    code = "research_backflow_fulfillment_not_found"
    message = "research backflow fulfillment not found"


class ResearchBackflowPlanNotFound(ResearchBackflowError):
    """backflow_plan_id 不存在。"""

    code = "research_backflow_plan_not_found"
    message = "research backflow plan not found"


class ResearchBackflowContinuationMismatch(ResearchBackflowError):
    """continuation identity（spec L）不符：新 SynthesisResult 的 company /
    research-question / analysis_as_of 与 request 绑定不一致。

    v1 不做 silent cutoff update——研究交接必须回到同一个研究问题 / 公司 / 时点，
    否则拒绝（0 write）。
    """

    code = "research_backflow_continuation_mismatch"
    message = "new synthesis result does not match the request identity/cutoff"


class ResearchBackflowNoProgress(ResearchBackflowError):
    """no-progress 政策（spec M）：新 SynthesisResult 与 source synthesis 相同。

    - 相同 result id（直接引用 source result）；
    - 相同 SynthesisRun fingerprint（同一 run 重分析，无新证据输入）。
    任一 → 拒绝（防止 research_required → 相同综合 → 无限循环）。
    """

    code = "research_backflow_no_progress"
    message = "new synthesis result is identical to the source synthesis"


class ResearchBackflowAlreadyFulfilled(ResearchBackflowError):
    """request 已有 fulfillment 且本次 result 不同（spec N）。

    不覆盖历史：同 request+result 走 replay；不同 result → 拒绝。
    """

    code = "research_backflow_already_fulfilled"
    message = "research request already fulfilled with a different synthesis result"


class ResearchBackflowIntegrityError(ResearchBackflowError):
    """Backflow verify integrity 重放校验失败基类（spec P，**不自动 repair**）。"""

    code = "research_backflow_integrity_error"
    message = "research backflow replay integrity error"


class ResearchBackflowPersistenceFailed(ResearchBackflowError):
    """Backflow 持久化事务失败（两表任一写入已整条回滚，0 partial write）。"""

    code = "research_backflow_persistence_failed"
    message = "research backflow persistence failed"
