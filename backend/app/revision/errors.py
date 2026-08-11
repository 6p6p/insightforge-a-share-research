"""Evidence-bound section revision error taxonomy (stage 5E.2A).

错误消息不包含：evidence 正文、完整 raw content、DB URL、UUID 集合明细、raw
provider response、prompt。`code` 是稳定错误码。integrity / not-found 错误由上游
`DraftSectionService` / `ReportCheckService` / `ReviewActionService` 抛出并原样
向上传播，本模块只定义 Revision 层的协调 / 校验错误。

**不 repair**：Revision 输入（source draft / trigger artifact / feedback / 输入
指纹）或输出（revised draft）被 tamper → `RevisionIntegrityError`，拒绝重放。
"""


class RevisionError(Exception):
    """Revision 域稳定错误基类。"""

    code = "revision_error"
    message = "revision error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message if message is not None else self.message)


class RevisionInputError(RevisionError):
    """调用方输入不合法（trigger union 非法 / revision_round < 1 等）。"""

    code = "revision_input_error"
    message = "invalid revision input"


class RevisionSourceNotFound(RevisionError):
    """source_draft_section_id 不存在 / 不可被 verified（上游已 raise 则透传）。"""

    code = "revision_source_not_found"
    message = "revision source draft section not found"


class RevisionTriggerInvalid(RevisionError):
    """trigger artifact 无法被 verified / action_type（或 human decision）不合法。

    例如 audit_rewrite 的 ReviewAction.action_type != rewrite、human_rewrite 的
    HumanDecision.decision != rewrite、deterministic_check 的 check 无任何 section
    相关 finding。触发对象是正式 immutable artifact，不猜测 / 不扩权。
    """

    code = "revision_trigger_invalid"
    message = "revision trigger artifact invalid"


class RevisionTargetSectionInvalid(RevisionError):
    """source DraftSection.section_id 不在 trigger 允许的 target sections（spec H）。

    - audit_rewrite：必须 ∈ ReviewAction.action_payload.target_section_ids；
    - human_rewrite：必须 ∈ HumanRequest.request_payload.section_ids；
    - deterministic_check：必须 ∈ CheckResult.findings 对应 section。
    """

    code = "revision_target_section_invalid"
    message = "source draft section not in trigger target sections"


class RevisionNotFound(RevisionError):
    """revision_id 不存在。"""

    code = "revision_not_found"
    message = "revision not found"


class RevisionIntegrityError(RevisionError):
    """`verify_revision_integrity` 重放校验失败（spec M，**不自动 repair**）。

    source DraftSection / trigger artifact / feedback / 输入指纹 / revised
    DraftSection 任一被 tamper → 拒绝。
    """

    code = "revision_integrity_error"
    message = "revision replay integrity error"


class RevisionPersistenceFailed(RevisionError):
    """Revision 持久化事务失败（draft_sections + draft_section_revisions 同事务，
    已整条回滚，0 partial write）。"""

    code = "revision_persistence_failed"
    message = "revision persistence failed"


class RevisionWriterModelUnavailable(RevisionError):
    """Revision writer model 不可用（provider 调用失败 / 未配置 / 懒加载缺失）。"""

    code = "revision_writer_model_unavailable"
    message = "revision writer model unavailable"


class RevisionWriterMalformedOutput(RevisionError):
    """Revision writer 的结构化输出不符合 WriterDecision / ParagraphCandidate schema。"""

    code = "revision_writer_malformed_output"
    message = "revision writer output failed schema validation"
