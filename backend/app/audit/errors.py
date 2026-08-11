"""Evidence-bound report audit error taxonomy (stage 5D).

错误消息不包含：evidence 正文、完整 raw content、DB URL、UUID 集合明细、raw
provider response、prompt。`code` 是稳定错误码。

integrity / not-found 错误由上游 `ReportService` / `ReportCheckService` 抛出
（`ReportIntegrityError` / `ReportNotFound` / `ReportCheckIntegrityError` /
`ReportCheckNotFound`）并原样向上传播，本模块只定义 Audit 层的协调 / 验证错误。
"""


class ReportAuditError(Exception):
    """Audit 域稳定错误基类。"""

    code = "report_audit_error"
    message = "report audit error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message if message is not None else self.message)


class ReportAuditInputError(ReportAuditError):
    """调用方输入不合法（report_id / check_result_id 非 UUID 等）。"""

    code = "report_audit_input_error"
    message = "invalid report audit request"


class ReportAuditNotFound(ReportAuditError):
    """audit_id 不存在。"""

    code = "report_audit_not_found"
    message = "report audit not found"


class ReportAuditCheckMismatch(ReportAuditError):
    """verified check 与 report 不匹配（verified_check.report_id != report_id）。"""

    code = "report_audit_check_mismatch"
    message = "report audit check result does not match report"


class ReportAuditMalformedOutput(ReportAuditError):
    """模型输出无法解析为 AuditDecision（schema 层失败，0 写）。"""

    code = "report_audit_malformed_output"
    message = "audit model output malformed"


class ReportAuditModelUnavailable(ReportAuditError):
    """模型不可用（未配置 / 构造为 None），调用前确定拒绝。"""

    code = "report_audit_model_unavailable"
    message = "audit model unavailable"


class ReportAuditValidationError(ReportAuditError):
    """AuditDecision 违反 hard validation（known / scope / coverage / enum）。"""

    code = "report_audit_validation_error"
    message = "audit decision validation error"


class ReportAuditParagraphOmitted(ReportAuditValidationError):
    """no-cherry-picking：reviewed_paragraph_refs 遗漏某个 P ref（不得少）。"""

    code = "report_audit_paragraph_omitted"
    message = "audit reviewed paragraph refs omitted a paragraph"


class ReportAuditUnknownRef(ReportAuditValidationError):
    """引用未知 S/P/C/E ref（不得有 unknown / cross-scope / fuzzy）。"""

    code = "report_audit_unknown_ref"
    message = "audit referenced unknown ref"


class ReportAuditParagraphDuplicate(ReportAuditValidationError):
    """reviewed_paragraph_refs 出现重复 P ref（不得重复）。"""

    code = "report_audit_paragraph_duplicate"
    message = "audit reviewed paragraph refs duplicated a paragraph"


class ReportAuditIntegrityError(ReportAuditError):
    """`verify_audit_integrity` 重放校验失败。

    重新 verify Report / CheckResult / rebuild Audit Pack / recompute
    audit_input_fingerprint / load ReviewIssues / 验证 refs / issue enums / scope /
    重派生 status / route / 重算 audit_fingerprint；任一损坏 → 拒绝
    （**不自动 repair**）。
    """

    code = "report_audit_integrity_error"
    message = "report audit replay integrity error"


class ReportAuditPersistenceFailed(ReportAuditError):
    """Audit 持久化事务失败（report_audits + review_issues 已整条回滚，0 partial write）。"""

    code = "report_audit_persistence_failed"
    message = "report audit persistence failed"
