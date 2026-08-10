"""Report outline error taxonomy (stage 5A).

错误消息不包含：evidence 正文、完整 raw content、DB URL、UUID 集合明细。
`code` 是稳定错误码。integrity / not-found 错误由上游
`SynthesisAnalysisService.verify_result_integrity` 抛出（`SynthesisResultIntegrityError`
/ `SynthesisAnalysisResultNotFound`）并原样向上传播，本模块只定义提纲派生
层的协调错误。
"""


class ReportOutlineError(Exception):
    """Report Outline 域稳定错误基类。"""

    code = "report_outline_error"
    message = "report outline error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message if message is not None else self.message)


class ReportOutlineNotFound(ReportOutlineError):
    """verify_outline_integrity 引用的 Outline 在 PG 中不存在。

    Writer 只消费已登记的提纲；outline 缺失 → 拒绝起草（不猜提纲 / 不自动
    创建）。"""

    code = "report_outline_not_found"
    message = "report outline not found"


class ReportOutlineClaimCoverageError(ReportOutlineError):
    """input Claims 未被提纲覆盖（既不在任何 theme section，也不是 duplicate_ref）。

    coverage 硬边界：确定性派生后，每个 input Claim 必须出现在某个 theme
    section 的 claim_ids 里，或是某个 duplicate 组的非 canonical 成员；否则
    提纲不完整 → 拒绝（**不静默丢 claim / 不猜主题**）。
    """

    code = "report_outline_claim_coverage_error"
    message = "report outline must cover every input claim"


class ReportOutlineIntegrityError(ReportOutlineError):
    """同 fingerprint 的既有提纲行与本次派生不一致（replay 校验失败）。

    fingerprint 已覆盖 schema / result / payload 全部派生字段；命中同指纹却
    payload 不同 → 数据被篡改 → 拒绝（**不自动 repair**）。
    """

    code = "report_outline_integrity_error"
    message = "report outline replay integrity error"


class ReportOutlinePersistenceFailed(ReportOutlineError):
    """提纲持久化事务失败（已整条回滚，0 partial write）。"""

    code = "report_outline_persistence_failed"
    message = "report outline persistence failed"
