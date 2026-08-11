"""derive 纯函数单测（stage 5E.1，spec F/G/H/I/J/K）：0 DB / 0 LLM。

覆盖：
- `derive_action_type`：pass/pass→finalize、fail/rewrite→rewrite、
  fail/research→research、fail/human_review→human_review；任何不一致
  （如 status=pass + route=research）→ `ReviewActionAuditInvalid`；
- `derive_action_payload`：finalize 只写 source ids；rewrite/human_review 写
  target_section_ids + review_issue_ids（canonical sort + dedupe）；research 额外
  写 related_claim_ids / related_evidence_card_ids / research_need_codes；提升
  route 保留**全部** issue ids / sections；0 issues 非 finalize 拒绝；
- `derive_human_request_payload`：issue_summaries 按 review_issue_ids 顺序、
  只含 issue_type/severity/section_id/paragraph_index（不复制 message / 证据）；
  非 human_review action 或缺失 issue id → `ReviewInputError`；
- `normalize_comment`：None/空白→None、trim、超 1000 → `ReviewInputError`。
"""

from uuid import uuid4

import pytest

from app.audit.contracts import (
    AUDIT_ROUTE_HUMAN_REVIEW,
    AUDIT_ROUTE_PASS,
    AUDIT_ROUTE_RESEARCH,
    AUDIT_ROUTE_REWRITE,
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
    RESEARCH_NEED_CODE_ADDITIONAL_EVIDENCE,
    RESEARCH_NEED_CODE_MISSING_SUPPORT,
    RESEARCH_NEED_CODE_STRONGER_SOURCE,
)
from app.review.derive import (
    derive_action_payload,
    derive_action_type,
    derive_human_request_payload,
    normalize_comment,
)
from app.review.errors import ReviewActionAuditInvalid, ReviewInputError


def _issue(
    *,
    issue_type: str,
    severity: str = "normal",
    section_id: str = "S1",
    paragraph_index: int = 0,
    claim_ids=(),
    evidence_ids=(),
) -> ReviewIssue:
    return ReviewIssue(
        review_issue_id=uuid4(),
        audit_id=uuid4(),
        ordinal=1,
        issue_type=issue_type,
        severity=severity,
        section_id=section_id,
        paragraph_index=paragraph_index,
        message="测试 issue",
        related_claim_ids=tuple(claim_ids),
        related_evidence_card_ids=tuple(evidence_ids),
    )


def _audit(*, issues: list[ReviewIssue], status: str, route: str) -> VerifiedReportAudit:
    """构造最小 `VerifiedReportAudit`（只填 derive 用到的字段；verified 链留空）。"""
    audit = object.__new__(VerifiedReportAudit)
    for field, value in {
        "audit_id": uuid4(),
        "report_id": uuid4(),
        "check_result_id": uuid4(),
        "audit_schema_version": 1,
        "auditor_name": "evidence_bound_report_auditor",
        "auditor_version": 1,
        "auditor_model_id": "deepseek:deepseek-v4-flash",
        "audit_input_fingerprint": "0" * 64,
        "audit_status": status,
        "recommended_route": route,
        "issue_count": len(issues),
        "audit_fingerprint": "0" * 64,
        "issues": tuple(issues),
        "verified_report": None,
        "verified_check": None,
    }.items():
        object.__setattr__(audit, field, value)
    return audit


def _rewrite_audit(*issues: ReviewIssue) -> VerifiedReportAudit:
    return _audit(
        issues=list(issues),
        status=AUDIT_STATUS_FAIL,
        route=AUDIT_ROUTE_REWRITE,
    )


# ---------------------------------------------------------------- derive_action_type（spec F）


def test_action_type_finalize() -> None:
    audit = _audit(issues=[], status=AUDIT_STATUS_PASS, route=AUDIT_ROUTE_PASS)
    assert derive_action_type(audit) == ACTION_TYPE_FINALIZE


def test_action_type_rewrite() -> None:
    audit = _rewrite_audit(_issue(issue_type="wording_overclaim"))
    assert derive_action_type(audit) == ACTION_TYPE_REWRITE


def test_action_type_research() -> None:
    audit = _audit(
        issues=[_issue(issue_type="unsupported_by_evidence")],
        status=AUDIT_STATUS_FAIL,
        route=AUDIT_ROUTE_RESEARCH,
    )
    assert derive_action_type(audit) == ACTION_TYPE_RESEARCH


def test_action_type_human_review() -> None:
    audit = _audit(
        issues=[_issue(issue_type="unresolved_conflict", severity="critical")],
        status=AUDIT_STATUS_FAIL,
        route=AUDIT_ROUTE_HUMAN_REVIEW,
    )
    assert derive_action_type(audit) == ACTION_TYPE_HUMAN_REVIEW


def test_action_type_inconsistent_audit_state_reject() -> None:
    """status=pass + route=research 不一致 → ReviewActionAuditInvalid（防御性硬边界）。"""
    audit = _audit(
        issues=[_issue(issue_type="unsupported_by_evidence")],
        status=AUDIT_STATUS_PASS,
        route=AUDIT_ROUTE_RESEARCH,
    )
    with pytest.raises(ReviewActionAuditInvalid):
        derive_action_type(audit)


# ---------------------------------------------------- derive_action_payload（spec G/H/I）


def test_action_payload_finalize_source_ids_only() -> None:
    audit = _audit(issues=[], status=AUDIT_STATUS_PASS, route=AUDIT_ROUTE_PASS)
    payload = derive_action_payload(audit, ACTION_TYPE_FINALIZE)
    assert payload == {
        "source_report_id": str(audit.report_id),
        "source_audit_id": str(audit.audit_id),
    }


def test_action_payload_finalize_with_issues_reject() -> None:
    audit = _rewrite_audit(_issue(issue_type="wording_overclaim"))
    with pytest.raises(ReviewActionAuditInvalid):
        derive_action_payload(audit, ACTION_TYPE_FINALIZE)


def test_action_payload_non_finalize_empty_issues_reject() -> None:
    audit = _audit(issues=[], status=AUDIT_STATUS_PASS, route=AUDIT_ROUTE_PASS)
    with pytest.raises(ReviewActionAuditInvalid):
        derive_action_payload(audit, ACTION_TYPE_REWRITE)


def test_action_payload_rewrite_sections_dedupe_and_order() -> None:
    i1 = _issue(issue_type="wording_overclaim", section_id="S1")
    i2 = _issue(issue_type="evidence_mismatch", section_id="S1")  # 同段同 section → dedupe
    i3 = _issue(issue_type="omitted_counterevidence", section_id="S2")
    audit = _rewrite_audit(i1, i2, i3)
    payload = derive_action_payload(audit, ACTION_TYPE_REWRITE)
    assert payload["target_section_ids"] == ["S1", "S2"]  # canonical sort + dedupe
    assert payload["review_issue_ids"] == sorted(
        str(issue.review_issue_id) for issue in (i1, i2, i3)
    )


def test_action_payload_research_need_codes_and_related_ids() -> None:
    i1 = _issue(
        issue_type="unsupported_by_evidence",
        claim_ids=("c2", "c1"),
        evidence_ids=("e2",),
    )
    i2 = _issue(
        issue_type="weak_source_quality",
        claim_ids=("c2",),
        evidence_ids=("e1", "e2"),
    )
    audit = _audit(
        issues=[i1, i2],
        status=AUDIT_STATUS_FAIL,
        route=AUDIT_ROUTE_RESEARCH,
    )
    payload = derive_action_payload(audit, ACTION_TYPE_RESEARCH)
    assert payload["research_need_codes"] == [
        RESEARCH_NEED_CODE_MISSING_SUPPORT,
        RESEARCH_NEED_CODE_STRONGER_SOURCE,
    ]
    assert payload["related_claim_ids"] == ["c1", "c2"]  # sorted union
    assert payload["related_evidence_card_ids"] == ["e1", "e2"]  # sorted union


def test_action_payload_elevated_research_preserves_all_issue_ids() -> None:
    """wording_overclaim（rewrite 类）+ insufficient_evidence（research 类）→ 最终
    research；提升后**保留全部** issue ids / sections（不丢 rewrite issue）。"""
    rewrite_issue = _issue(issue_type="wording_overclaim", section_id="S1")
    research_issue = _issue(issue_type="insufficient_evidence", section_id="S2")
    audit = _audit(
        issues=[rewrite_issue, research_issue],
        status=AUDIT_STATUS_FAIL,
        route=AUDIT_ROUTE_RESEARCH,
    )
    payload = derive_action_payload(audit, ACTION_TYPE_RESEARCH)
    assert payload["review_issue_ids"] == sorted(
        str(issue.review_issue_id) for issue in (rewrite_issue, research_issue)
    )
    assert payload["target_section_ids"] == ["S1", "S2"]
    # research 专属字段只来自 research 类 issue。
    assert payload["research_need_codes"] == [RESEARCH_NEED_CODE_ADDITIONAL_EVIDENCE]


def test_action_payload_elevated_human_review_keeps_all_sections() -> None:
    """unresolved_conflict critical + wording_overclaim → human_review；保留全部。"""
    conflict_issue = _issue(issue_type="unresolved_conflict", severity="critical", section_id="S1")
    rewrite_issue = _issue(issue_type="wording_overclaim", section_id="S2")
    audit = _audit(
        issues=[conflict_issue, rewrite_issue],
        status=AUDIT_STATUS_FAIL,
        route=AUDIT_ROUTE_HUMAN_REVIEW,
    )
    payload = derive_action_payload(audit, ACTION_TYPE_HUMAN_REVIEW)
    assert payload["review_issue_ids"] == sorted(
        str(issue.review_issue_id) for issue in (conflict_issue, rewrite_issue)
    )
    assert payload["target_section_ids"] == ["S1", "S2"]
    assert set(payload) == {
        "source_report_id",
        "source_audit_id",
        "target_section_ids",
        "review_issue_ids",
    }


# ------------------------------------------------------- derive_human_request_payload（spec J）


def test_human_request_payload_summaries_in_issue_id_order() -> None:
    i1 = _issue(
        issue_type="unresolved_conflict",
        severity="critical",
        section_id="S1",
        paragraph_index=2,
    )
    i2 = _issue(
        issue_type="unresolved_conflict",
        severity="high",
        section_id="S2",
        paragraph_index=0,
    )
    audit = _audit(
        issues=[i1, i2],
        status=AUDIT_STATUS_FAIL,
        route=AUDIT_ROUTE_HUMAN_REVIEW,
    )
    action_payload = derive_action_payload(audit, ACTION_TYPE_HUMAN_REVIEW)
    payload = derive_human_request_payload(audit, ACTION_TYPE_HUMAN_REVIEW, action_payload)
    assert payload["report_id"] == str(audit.report_id)
    assert payload["audit_id"] == str(audit.audit_id)
    # issue_summaries 按 review_issue_ids 顺序（canonical sorted）。
    assert payload["review_issue_ids"] == sorted(str(i.review_issue_id) for i in (i1, i2))
    assert payload["section_ids"] == ["S1", "S2"]
    by_id = {str(i.review_issue_id): i for i in (i1, i2)}
    assert len(payload["issue_summaries"]) == 2
    for raw_id, summary in zip(
        payload["review_issue_ids"], payload["issue_summaries"], strict=True
    ):
        # 只存身份字段，不复制 message / claim / evidence 详情。
        assert set(summary) == {"issue_type", "severity", "section_id", "paragraph_index"}
        assert summary["issue_type"] == by_id[raw_id].issue_type
        assert summary["severity"] == by_id[raw_id].severity
        assert summary["section_id"] == by_id[raw_id].section_id
        assert summary["paragraph_index"] == by_id[raw_id].paragraph_index


def test_human_request_payload_requires_human_review_action() -> None:
    audit = _audit(issues=[], status=AUDIT_STATUS_PASS, route=AUDIT_ROUTE_PASS)
    with pytest.raises(ReviewInputError):
        derive_human_request_payload(audit, ACTION_TYPE_FINALIZE, {})


def test_human_request_payload_missing_issue_id_reject() -> None:
    audit = _audit(
        issues=[_issue(issue_type="unresolved_conflict", severity="critical")],
        status=AUDIT_STATUS_FAIL,
        route=AUDIT_ROUTE_HUMAN_REVIEW,
    )
    action_payload = {"review_issue_ids": [str(uuid4())], "target_section_ids": ["S1"]}
    with pytest.raises(ReviewInputError):
        derive_human_request_payload(audit, ACTION_TYPE_HUMAN_REVIEW, action_payload)


# ---------------------------------------------------------------- normalize_comment（spec K）


def test_normalize_comment_none_and_blank() -> None:
    assert normalize_comment(None) is None
    assert normalize_comment("") is None
    assert normalize_comment("   ") is None


def test_normalize_comment_trims() -> None:
    assert normalize_comment("  已人工核对  ") == "已人工核对"


def test_normalize_comment_too_long_reject() -> None:
    with pytest.raises(ReviewInputError):
        normalize_comment("x" * (MAX_COMMENT_LENGTH + 1))
