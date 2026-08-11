"""Deterministic revision derivation (stage 5E.2A, spec G/H/I).

纯函数：trigger 分类、target section 校验、section-normalized feedback 派生。
**0 LLM / 0 检索**——trigger artifact 已由上游 service verify（Read-side 公共
API），本模块只做确定性投影。

- trigger 三选一（spec G）：check_result_id → deterministic_check；
  review_action_id → audit_rewrite；human_decision_id → human_rewrite；
- target section（spec H）：
  - audit_rewrite：source section ∈ ReviewAction.action_payload.target_section_ids；
  - human_rewrite：经 HumanDecision→HumanRequest→ReviewAction 恢复
    action_payload.target_section_ids（underlying action 是 human_review，其
    payload 同样携带 target_section_ids）；
  - deterministic_check：source section ∈ CheckResult.findings 出现的 section 集；
- feedback（spec I，全部视为 DATA，**不含** issue/finding id 与正文段落全文）：
  - deterministic_check：section 相关 finding code + paragraph_index；
  - audit_rewrite：section 相关 issue 的 issue_type/severity/paragraph_index/message；
  - human_rewrite：underlying section issues + 末尾一条 human comment。
"""

from app.audit.contracts import VerifiedReportAudit
from app.report.contracts import VerifiedReportCheckResult
from app.revision.contracts import (
    FEEDBACK_CODE_HUMAN_COMMENT,
    TRIGGER_TYPE_AUDIT_REWRITE,
    TRIGGER_TYPE_DETERMINISTIC_CHECK,
    TRIGGER_TYPE_HUMAN_REWRITE,
    RevisionFeedbackItem,
    RevisionTrigger,
)
from app.revision.errors import (
    RevisionInputError,
    RevisionTargetSectionInvalid,
    RevisionTriggerInvalid,
)


def derive_trigger_type(trigger: RevisionTrigger) -> str:
    """trigger 三选一 → trigger_type（`RevisionTrigger` 构造已强约束；此处兜底）。"""
    if trigger.check_result_id is not None:
        return TRIGGER_TYPE_DETERMINISTIC_CHECK
    if trigger.review_action_id is not None:
        return TRIGGER_TYPE_AUDIT_REWRITE
    if trigger.human_decision_id is not None:
        return TRIGGER_TYPE_HUMAN_REWRITE
    raise RevisionInputError("trigger 必须恰好一个非空（check/action/decision 三选一）")


def check_target_section_ids(
    verified_check: VerifiedReportCheckResult,
) -> tuple[str, ...]:
    """deterministic_check 的目标 section 集 = findings 出现的 section（spec H）。

    无 section 的 finding（report 级）不能作为修订目标。
    """
    sections = {finding.section_id for finding in verified_check.findings if finding.section_id}
    return tuple(sorted(sections))


def action_target_section_ids(action_payload: dict) -> tuple[str, ...]:
    """audit_rewrite / human_rewrite 的目标 section 集 = action_payload。

    注意：human_rewrite 的 underlying action 是 human_review——其 payload 同样
    携带 target_section_ids（derive_action_payload 对 rewrite/human_review 都写入）。
    """
    raw = action_payload.get("target_section_ids")
    if not isinstance(raw, list) or not raw:
        raise RevisionTriggerInvalid("review action payload 缺少 target_section_ids")
    return tuple(sorted({str(section_id) for section_id in raw}))


def validate_target_section(
    source_section_id: str,
    target_section_ids: tuple[str, ...],
) -> None:
    """spec H：source section 必须 ∈ trigger 的目标 section 集，否则拒绝（0 write）。"""
    if source_section_id not in target_section_ids:
        raise RevisionTargetSectionInvalid(
            f"source section {source_section_id!r} not in trigger target sections"
        )


def derive_check_feedback(
    verified_check: VerifiedReportCheckResult,
    section_id: str,
) -> tuple[RevisionFeedbackItem, ...]:
    """deterministic_check feedback（spec I）：section 相关 finding code + paragraph_index。

    finding 不含 message（spec R：不存长 prose），feedback 只给 code + 段落索引，
    由修订 writer 对照该 section 的 C/E/X/G 数据自行定位问题。
    """
    items: list[RevisionFeedbackItem] = []
    for finding in verified_check.findings:
        if finding.section_id != section_id:
            continue
        items.append(
            RevisionFeedbackItem(
                trigger_type=TRIGGER_TYPE_DETERMINISTIC_CHECK,
                code=finding.code,
                paragraph_index=finding.paragraph_index,
            )
        )
    return tuple(items)


def derive_audit_feedback(
    verified_audit: VerifiedReportAudit,
    section_id: str,
) -> tuple[RevisionFeedbackItem, ...]:
    """audit_rewrite feedback（spec I）：section 相关 issue 的完整投影。

    ordinal 顺序保持（spec R 已确定性排序）；只投影 issue_type / severity /
    paragraph_index / message，**不含** issue id / related ids。
    """
    items: list[RevisionFeedbackItem] = []
    for issue in verified_audit.issues:
        if issue.section_id != section_id:
            continue
        items.append(
            RevisionFeedbackItem(
                trigger_type=TRIGGER_TYPE_AUDIT_REWRITE,
                code=issue.issue_type,
                severity=issue.severity,
                paragraph_index=issue.paragraph_index,
                message=issue.message,
            )
        )
    return tuple(items)


def derive_human_feedback(
    verified_audit: VerifiedReportAudit,
    section_id: str,
    comment: str | None,
) -> tuple[RevisionFeedbackItem, ...]:
    """human_rewrite feedback（spec I）：underlying section issues + human comment。

    comment 非空时追加一条 `code=human_comment` 的反馈（置于 issues 之后）；为空
    则只发 underlying issues。
    """
    items = list(derive_audit_feedback(verified_audit, section_id))
    if comment:
        items.append(
            RevisionFeedbackItem(
                trigger_type=TRIGGER_TYPE_HUMAN_REWRITE,
                code=FEEDBACK_CODE_HUMAN_COMMENT,
                message=comment,
            )
        )
    return tuple(items)
