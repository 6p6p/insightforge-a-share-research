"""Deterministic report assembly + check error taxonomy (stage 5C).

错误消息不包含：evidence 正文、完整 raw content、DB URL、UUID 集合明细、raw
provider response。`code` 是稳定错误码。

integrity / not-found 错误由上游 `ReportOutlineService` / `DraftSectionService`
抛出（`ReportOutlineIntegrityError` / `ReportOutlineNotFound` /
`DraftSectionIntegrityError` / `DraftSectionNotFound` /
`DraftSectionLegacyVersionUnsupported`）并原样向上传播，本模块只定义 Report
装配 / 检查层的协调错误。
"""


class ReportError(Exception):
    """Report 域稳定错误基类。"""

    code = "report_error"
    message = "report error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message if message is not None else self.message)


class ReportInputError(ReportError):
    """调用方输入不合法（outline_id 非 UUID / draft_section_ids 为空等）。

    可在调用任何 DB / LLM 前确定性拒绝。
    """

    code = "report_input_error"
    message = "invalid report assembly request"


class ReportNotFound(ReportError):
    """report_id 不存在。

    Report 是不可变确定性产物；缺失 → 拒绝（不自动重建 / 不猜）。"""

    code = "report_not_found"
    message = "report not found"


class ReportAssemblyError(ReportError):
    """装配 coverage / identity 不匹配。

    - 每个 DraftSection 的 outline_id 必须等于 input outline；
    - 每个 Outline section 恰好一个 DraftSection（missing / duplicate / extra）；
    - section_id / order / type / title 必须与 Outline 完全一致。
    任一违反 → 拒绝装配（0 写）。
    """

    code = "report_assembly_error"
    message = "report assembly coverage or identity mismatch"


class ReportIntegrityError(ReportError):
    """`verify_report_integrity` 重放校验失败。

    重新 verify Outline + 全部 selected DraftSections + rebuild exact payload +
    重算 fingerprint；任一 text / section / draft id / metadata / fingerprint 被
    SQL tamper → 拒绝（**不自动 repair**）。
    """

    code = "report_integrity_error"
    message = "report replay integrity error"


class ReportPersistenceFailed(ReportError):
    """Report 持久化事务失败（已整条回滚，0 partial write）。"""

    code = "report_persistence_failed"
    message = "report persistence failed"


class ReportCheckNotFound(ReportError):
    """check result 不存在。"""

    code = "report_check_not_found"
    message = "report check result not found"


class ReportCheckPersistenceFailed(ReportError):
    """CheckResult 持久化事务失败（已整条回滚，0 partial write）。"""

    code = "report_check_persistence_failed"
    message = "report check persistence failed"


class ReportCheckIntegrityError(ReportError):
    """`verify_check_result_integrity` 重放校验失败。

    重新 verify 上游 Report + 重跑确定性 checks（重算 expected status / findings /
    check_fingerprint）；任一 status / findings / fingerprint / schema / report_id
    被 SQL tamper → 拒绝（**不自动 repair**）。status 不在 check_fingerprint 内，
    必须重跑 checks 才能发现 pass/fail 篡改。
    """

    code = "report_check_integrity_error"
    message = "report check replay integrity error"
