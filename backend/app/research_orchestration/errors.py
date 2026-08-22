"""Research orchestration error tree (stage 7A.2B.1).

- `ResearchOrchestrationError`：领域错误基类（继承 `app.core.errors.DomainError`）；
- `ResearchOrchestrationNotFound`：orchestration 不存在（404）；
- `ResearchOrchestrationActiveConflict`：同 task 已存在 active orchestration（409）
  —— 新 fingerprint 的 create 在已有 active orchestration 时拒绝；
- `ResearchOrchestrationIntegrityError`：orchestration 完整性校验失败（tamper /
  上游 mismatch，409）；
- `ResearchOrchestrationChildNotFound`：exact child `(orchestration_id, stage,
  attempt_no)` 不存在（404）；
- `ResearchOrchestrationChildConflict`：child 已存在 / 已被其他 orchestration 拥有
  （409，UNIQUE(workflow_run_id) / UNIQUE(orchestration_id, stage, attempt_no)）；
- `ResearchOrchestrationAlreadyFinished`：对 terminal orchestration 执行 cancel /
  重新执行（409）；
- `ResearchOrchestrationInvalidAction`（7A.2B.2 spec N/P）：`act_on_orchestration`
  对当前状态不允许的操作（非 waiting_human / 非 awaiting_stage5 / 未知 action，400）。
"""

from app.core.errors import DomainError


class ResearchOrchestrationError(DomainError):
    """research orchestration 领域错误基类。"""

    code = "research_orchestration_error"
    http_status = 500
    message = "研究编排错误"


class ResearchOrchestrationNotFound(ResearchOrchestrationError):
    """research_orchestration_runs 不存在。"""

    code = "research_orchestration_not_found"
    http_status = 404
    message = "研究编排不存在"


class ResearchOrchestrationActiveConflict(ResearchOrchestrationError):
    """同 task 已存在 active orchestration（partial unique index 兜底）。"""

    code = "research_orchestration_active_conflict"
    http_status = 409
    message = "该任务已存在进行中的研究编排"


class ResearchOrchestrationIntegrityError(ResearchOrchestrationError):
    """orchestration 完整性校验失败（fingerprint mismatch / 上游 mismatch）。"""

    code = "research_orchestration_integrity_error"
    http_status = 409
    message = "研究编排完整性校验失败"


class ResearchOrchestrationChildNotFound(ResearchOrchestrationError):
    """exact child (orchestration_id, stage, attempt_no) 不存在。"""

    code = "research_orchestration_child_not_found"
    http_status = 404
    message = "研究编排子工作流不存在"


class ResearchOrchestrationChildConflict(ResearchOrchestrationError):
    """child 归属冲突：WorkflowRun 已被其他 orchestration 拥有 / 同 scope 重复。"""

    code = "research_orchestration_child_conflict"
    http_status = 409
    message = "研究编排子工作流归属冲突"


class ResearchOrchestrationAlreadyFinished(ResearchOrchestrationError):
    """orchestration 已结束（completed/failed/cancelled），不能重复 cancel/执行。"""

    code = "research_orchestration_already_finished"
    http_status = 409
    message = "研究编排已结束，不能重复执行"


class ResearchOrchestrationInvalidAction(ResearchOrchestrationError):
    """`act_on_orchestration` 对当前状态不允许的操作（7A.2B.2 spec N/P）。

    覆盖：非 waiting_human 提交 human decision、非 awaiting_stage5 阶段、
    未知 action 名。
    """

    code = "research_orchestration_invalid_action"
    http_status = 400
    message = "研究编排当前状态不允许该操作"


class ResearchOrchestrationRetryRequired(ResearchOrchestrationError):
    """task 无 active 且最近一次 orchestration 是 failed/cancelled（Gate C Case 6）。

    自动研究入口**不偷偷回到 attempt1**——返回 409，用户必须显式 retry
    （`POST /research-orchestrations/{id}/actions` action=retry → 新 attempt）。
    """

    code = "research_orchestration_retry_required"
    http_status = 409
    message = "该任务最近一次研究编排已结束且未成功，请显式重试"


class ResearchOrchestrationApprovalRejected(ResearchOrchestrationError):
    """人工 approve 提交被确定性阻断（Stage5 finalize 抛
    `Stage5ApproveRequiresPassCheck`，**仅系统级不可恢复错误**：
    artifact 缺失 / 审核记录损坏 / 数据库一致性破坏 / 状态损坏）。

    v1.2.5 风险提示系统：内容审核问题（含 CRITICAL_ALERT 严重提醒）不再触发本
    错误——approve 总被接受（带提醒完成 completed_with_warnings）。本错误仅在
    系统级失败时把 orchestration 投影为 failed（与 Stage5 child run 的 FAILED
    终态一致），并返回可理解的 409——不再裸 500、不再让 UI 卡在 waiting_human
    后二次点击报「工作流已结束」。
    """

    code = "research_orchestration_approval_rejected"
    http_status = 409
    message = "报告未能批准：存在系统级审核故障（审核记录缺失或数据一致性异常），请重试或重新研究"
