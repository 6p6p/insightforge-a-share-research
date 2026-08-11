"""Report review routing + human confirmation error taxonomy (stage 5E.1).

错误消息不包含：evidence 正文、完整 raw content、DB URL、UUID 集合明细、raw
provider response、prompt。`code` 是稳定错误码。

integrity / not-found 错误由上游 `ReportAuditService` 抛出并原样向上传播，本模块
只定义 Review 层的协调 / 验证错误。
"""


class ReviewError(Exception):
    """Review 域稳定错误基类。"""

    code = "review_error"
    message = "review error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message if message is not None else self.message)


class ReviewInputError(ReviewError):
    """调用方输入不合法（decision 枚举 / comment 超长等）。"""

    code = "review_input_error"
    message = "invalid review input"


class ReviewActionAuditInvalid(ReviewError):
    """audit status / recommended_route 无法映射到合法 action_type（spec F）。

    `verify_audit_integrity` 已保证组合合法（pass/pass 或 fail/<route>），此处为
    防御性硬边界：任何不一致（如 status=pass + route=research）→ 拒绝。
    """

    code = "review_action_audit_invalid"
    message = "review action audit status/route invalid"


class ReviewActionCheckNotPass(ReviewError):
    """finalize 安全门禁（spec A Gate 0）：deterministic CheckResult 未通过。

    finalize 必须同时通过 Check + Audit：除 audit_status=pass +
    recommended_route=pass 外，`VerifiedReportAudit.verified_check.status` 必须也是
    `pass`。Agent Audit 不得覆盖 deterministic Check failure——Check=fail 时即使
    Audit 人为/fixture=pass 也必须拒绝 finalize（0 ReviewAction write）。
    """

    code = "review_action_check_not_pass"
    message = "review action finalize requires deterministic check to pass"


class ReviewActionNotFound(ReviewError):
    """review_action_id 不存在。"""

    code = "review_action_not_found"
    message = "review action not found"


class ReviewRequestNotHumanReview(ReviewError):
    """只有 action_type=human_review 才能创建 human review request（spec J）。"""

    code = "review_request_not_human_review"
    message = "human review request requires a human_review action"


class HumanReviewRequestNotFound(ReviewError):
    """human_request_id 不存在。"""

    code = "human_review_request_not_found"
    message = "human review request not found"


class HumanReviewDecisionNotFound(ReviewError):
    """human_decision_id 不存在。"""

    code = "human_review_decision_not_found"
    message = "human review decision not found"


class HumanReviewAlreadyResolved(ReviewError):
    """request 已有不同 decision / comment 的 immutable decision（spec K）。

    不覆盖历史：同完全相同 decision/comment 走 replay，不同则拒绝。
    """

    code = "human_review_already_resolved"
    message = "human review request already resolved with a different decision"


class ReviewIntegrityError(ReviewError):
    """Review 层 verify integrity 重放校验失败基类（spec N，**不自动 repair**）。"""

    code = "review_integrity_error"
    message = "review integrity error"


class ReviewActionIntegrityError(ReviewIntegrityError):
    """`verify_review_action_integrity` 失败：重 verify Audit → 重派生 action →
    重算指纹 → 对比 persisted；任一损坏 → 拒绝。"""

    code = "review_action_integrity_error"
    message = "review action replay integrity error"


class HumanReviewRequestIntegrityError(ReviewIntegrityError):
    """`verify_human_request_integrity` 失败（上游 action 或 payload 被 tamper）。"""

    code = "human_review_request_integrity_error"
    message = "human review request replay integrity error"


class HumanReviewDecisionIntegrityError(ReviewIntegrityError):
    """`verify_human_decision_integrity` 失败（上游 request 或 immutable fields 被 tamper）。"""

    code = "human_review_decision_integrity_error"
    message = "human review decision replay integrity error"


class ReviewPersistenceFailed(ReviewError):
    """Review 持久化事务失败（三表任一写入已整条回滚，0 partial write）。"""

    code = "review_persistence_failed"
    message = "review persistence failed"
