"""Deterministic review routing derivation (stage 5E.1, spec F/G/H/I/J).

从 `VerifiedReportAudit` 纯函数派生 action_type + action_payload + human request
payload。**0 LLM / 0 检索 / 0 Chroma / 0 rewrite / 0 research**——只建立稳定、
可回放、可审计的控制层 artifact。

映射（spec F，严格确定性）：
- `(pass, pass)` → `finalize`；
- `(fail, rewrite)` → `rewrite`；
- `(fail, research)` → `research`；
- `(fail, human_review)` → `human_review`。

target_section_ids（spec H）：取对应 action 类的 ReviewIssues 的真实 section_id，
canonical sort + dedupe。若整个 Audit 因 route priority 提升（存在不属于最终
action 类的其他 issues，例如同时 wording_overclaim + insufficient_evidence → 最终
research），则 `review_issue_ids` 保留**全部** issues、`target_section_ids` 包含
**全部** affected sections（不丢 rewrite issue——5E.2 research 完成后仍可能
rewrite）。

research 专属（spec I）：`research_need_codes` 由 issue_type canonical 映射
（unsupported_by_evidence→missing_support 等）；`related_claim_ids` /
`related_evidence_card_ids` 只取 research 类 issues 的并集，供未来 Research
Planner 生成任务（本阶段不自动生成搜索 query / 不调 Chroma / 不访问 web）。
"""

from app.audit.contracts import (
    AUDIT_ROUTE_HUMAN_REVIEW,
    AUDIT_ROUTE_PASS,
    AUDIT_ROUTE_RESEARCH,
    AUDIT_ROUTE_REWRITE,
    AUDIT_SEVERITY_CRITICAL,
    AUDIT_SEVERITY_HIGH,
    AUDIT_SEVERITY_NORMAL,
    AUDIT_STATUS_FAIL,
    AUDIT_STATUS_PASS,
    ReviewIssue,
    VerifiedReportAudit,
)
from app.review.contracts import (
    ACTION_TYPE_FINALIZE,
    ACTION_TYPE_HUMAN_REVIEW,
    ACTION_TYPE_RESEARCH,
    ACTION_TYPE_REWRITE,
    MAX_COMMENT_LENGTH,
    RESEARCH_NEED_CODE_BY_ISSUE_TYPE,
)
from app.review.errors import ReviewActionAuditInvalid, ReviewInputError

# issue_type 常量（与 app/audit/contracts.AUDIT_ISSUE_TYPES 同步维护）。
_ISSUE_UNRESOLVED_CONFLICT = "unresolved_conflict"

_REWRITE_ISSUE_TYPES = frozenset(
    {
        "evidence_mismatch",
        "claim_misrepresentation",
        "wording_overclaim",
        "omitted_counterevidence",
        "causal_overreach",
        "valuation_overreach",
    }
)
_RESEARCH_ISSUE_TYPES = frozenset(RESEARCH_NEED_CODE_BY_ISSUE_TYPE)


def _issue_in_action_class(issue: ReviewIssue, action_type: str) -> bool:
    """单条 issue 是否属于某 action_type 的 route 类（spec H 分类）。"""
    if action_type == ACTION_TYPE_REWRITE:
        return issue.issue_type in _REWRITE_ISSUE_TYPES or (
            issue.issue_type == _ISSUE_UNRESOLVED_CONFLICT
            and issue.severity == AUDIT_SEVERITY_NORMAL
        )
    if action_type == ACTION_TYPE_RESEARCH:
        return issue.issue_type in _RESEARCH_ISSUE_TYPES
    if action_type == ACTION_TYPE_HUMAN_REVIEW:
        return issue.issue_type == _ISSUE_UNRESOLVED_CONFLICT and issue.severity in (
            AUDIT_SEVERITY_HIGH,
            AUDIT_SEVERITY_CRITICAL,
        )
    return False


def derive_action_type(verified_audit: VerifiedReportAudit) -> str:
    """spec F：audit status / recommended_route → action_type（严格确定性）。"""
    status = verified_audit.audit_status
    route = verified_audit.recommended_route
    if status == AUDIT_STATUS_PASS and route == AUDIT_ROUTE_PASS:
        return ACTION_TYPE_FINALIZE
    if status == AUDIT_STATUS_FAIL and route == AUDIT_ROUTE_REWRITE:
        return ACTION_TYPE_REWRITE
    if status == AUDIT_STATUS_FAIL and route == AUDIT_ROUTE_RESEARCH:
        return ACTION_TYPE_RESEARCH
    if status == AUDIT_STATUS_FAIL and route == AUDIT_ROUTE_HUMAN_REVIEW:
        return ACTION_TYPE_HUMAN_REVIEW
    raise ReviewActionAuditInvalid()


def derive_action_payload(verified_audit: VerifiedReportAudit, action_type: str) -> dict:
    """spec G：从 Verified Audit + ReviewIssues 派生 action_payload（caller 不提供字段）。

    - finalize：只有 source ids（pass → 0 issues）；
    - rewrite / human_review：source ids + target_section_ids + review_issue_ids；
    - research：额外含 related_claim_ids / related_evidence_card_ids /
      research_need_codes。
    """
    issues = list(verified_audit.issues)
    if action_type == ACTION_TYPE_FINALIZE:
        if issues:
            raise ReviewActionAuditInvalid()
        return {
            "source_report_id": str(verified_audit.report_id),
            "source_audit_id": str(verified_audit.audit_id),
        }
    if not issues:
        raise ReviewActionAuditInvalid()

    matching = [issue for issue in issues if _issue_in_action_class(issue, action_type)]
    elevated = len(matching) != len(issues)
    selected = issues if elevated else matching

    review_issue_ids = sorted(str(issue.review_issue_id) for issue in selected)
    target_section_ids = sorted({issue.section_id for issue in selected})

    payload: dict = {
        "source_report_id": str(verified_audit.report_id),
        "source_audit_id": str(verified_audit.audit_id),
        "target_section_ids": target_section_ids,
        "review_issue_ids": review_issue_ids,
    }
    if action_type == ACTION_TYPE_RESEARCH:
        research_issues = [issue for issue in issues if issue.issue_type in _RESEARCH_ISSUE_TYPES]
        if not research_issues:
            # route=research 必有 research 类 issue（derive_route 保证）；防御性兜底。
            raise ReviewActionAuditInvalid()
        payload.update(
            {
                "related_claim_ids": sorted(
                    {claim_id for issue in research_issues for claim_id in issue.related_claim_ids}
                ),
                "related_evidence_card_ids": sorted(
                    {
                        evidence_id
                        for issue in research_issues
                        for evidence_id in issue.related_evidence_card_ids
                    }
                ),
                "research_need_codes": sorted(
                    {
                        RESEARCH_NEED_CODE_BY_ISSUE_TYPE[issue.issue_type]
                        for issue in research_issues
                    }
                ),
            }
        )
    return payload


def derive_human_request_payload(
    verified_audit: VerifiedReportAudit,
    action_type: str,
    action_payload: dict,
) -> dict:
    """spec J：human review request payload（只存 IDs + issue summaries）。

    不把 Evidence quote / 完整 paragraph / prompt 复制进去——Web 后续按 IDs 再
    加载详情。issue_summaries 按 action_payload 的 review_issue_ids 顺序派生。
    """
    if action_type != ACTION_TYPE_HUMAN_REVIEW:
        raise ReviewInputError("human review request requires a human_review action")
    review_issue_ids = list(action_payload["review_issue_ids"])
    by_id = {str(issue.review_issue_id): issue for issue in verified_audit.issues}
    summaries: list[dict] = []
    for raw_id in review_issue_ids:
        issue = by_id.get(raw_id)
        if issue is None:
            raise ReviewInputError("review issue id missing from verified audit")
        summaries.append(
            {
                "issue_type": issue.issue_type,
                "severity": issue.severity,
                "section_id": issue.section_id,
                "paragraph_index": issue.paragraph_index,
            }
        )
    return {
        "report_id": str(verified_audit.report_id),
        "audit_id": str(verified_audit.audit_id),
        "review_issue_ids": review_issue_ids,
        "section_ids": list(action_payload["target_section_ids"]),
        "issue_summaries": summaries,
    }


def normalize_comment(comment: str | None) -> str | None:
    """spec K：comment trim；trim 后为空 → None；超 1000 字符 → ReviewInputError。"""
    if comment is None:
        return None
    if not isinstance(comment, str):
        raise ReviewInputError("comment 必须是字符串")
    trimmed = comment.strip()
    if not trimmed:
        return None
    if len(trimmed) > MAX_COMMENT_LENGTH:
        raise ReviewInputError(f"comment 超长（>{MAX_COMMENT_LENGTH} 字符）")
    return trimmed
