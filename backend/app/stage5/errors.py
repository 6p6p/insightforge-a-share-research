"""Stable error taxonomy for the Stage 5 report control workflow (spec 5E.2A O-Q).

错误消息不包含：evidence 正文、完整 raw content、prompt、UUID 集合明细。
`code` 是稳定错误码。graph 节点内部各服务（Outline / DraftSection / Report /
Check / Audit / Review / Revision）抛出的域错误原样向上传播，本模块只定义
graph 编排层的协调错误。
"""


class Stage5WorkflowError(Exception):
    """Stage 5 工作流顶层错误基类。"""

    code = "stage5_workflow_error"
    message = "stage 5 workflow error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message if message is not None else self.message)


class Stage5ResearchTaskNotFound(Stage5WorkflowError):
    """Stage5WorkflowRequest.task_id 引用的 ResearchTask 在 PG 中不存在。

    创建 run 前真实 PG 校验：任务缺失 → 拒绝创建（**不**自动创建 fake
    ResearchTask / 不猜任务）。Stage 5 WorkflowRun 必须绑定一个真实任务。
    """

    code = "stage5_research_task_not_found"
    message = "research task not found for stage 5 run"


class Stage5InvalidState(Stage5WorkflowError):
    """graph state 形状非法（防御性兜底；请求构造已做完整校验）。

    不包含具体 UUID / 正文明细；只报缺失的 state key。
    """

    code = "stage5_invalid_state"
    message = "invalid stage 5 workflow state"


class Stage5InvalidHumanResume(Stage5WorkflowError):
    """human resume 载荷非法（decision 枚举 / payload 形状不匹配）。"""

    code = "stage5_invalid_human_resume"
    message = "invalid stage 5 human resume payload"


class Stage5NoPendingHumanReview(Stage5WorkflowError):
    """run 处于 WAITING_HUMAN，但 checkpoint 中没有待裁决的 human review。

    通常是 state 被外部篡改或 graph 版本不一致；拒绝继续，避免对错误的人审
    request 写入 decision。
    """

    code = "stage5_no_pending_human_review"
    message = "run has no pending human review request"


class Stage5ApproveRequiresPassCheck(Stage5WorkflowError):
    """spec R：人工 approve 只能 finalize 当前 Report，且前提 deterministic
    Check=pass（Gate 0 不被人工裁决覆盖）。

    人审 route 是 audit_status=fail 的 unresolved_conflict（high/critical），
    Check 可能 pass 也可能 fail；Check=fail 时 approve 不得 finalize——直接失败，
    不静默改写为其他动作。
    """

    code = "stage5_approve_requires_pass_check"
    message = "human approve requires deterministic check pass"
