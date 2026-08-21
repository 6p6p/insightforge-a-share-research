"""AuditSeverity 确定性分类单元测试（v1.2.3 §2/§3）。

验证：LLM 不参与 —— severity 完全由 issue_type / check finding code + degraded
属性确定性推导；保守映射（未知 → critical；degraded → warning；绝不降级 critical）。
纯函数，零 DB / 零网络。
"""

from app.audit.severity import (
    AuditSeverity,
    audit_issue_severity,
    check_finding_severity,
    classify_report_severity,
    severity_rank,
    stricter,
)


class FakeIssue:
    def __init__(self, issue_type: str, section_id: str | None = None):
        self.issue_type = issue_type
        self.section_id = section_id


def test_severity_ranks_strict_monotonic() -> None:
    assert severity_rank(AuditSeverity.INFO) < severity_rank(AuditSeverity.WARNING)
    assert severity_rank(AuditSeverity.WARNING) < severity_rank(AuditSeverity.CRITICAL)


def test_stricter_keeps_critical_over_warning() -> None:
    assert stricter(AuditSeverity.WARNING, AuditSeverity.CRITICAL) is AuditSeverity.CRITICAL
    assert stricter(AuditSeverity.CRITICAL, AuditSeverity.INFO) is AuditSeverity.CRITICAL
    assert stricter(AuditSeverity.INFO, AuditSeverity.WARNING) is AuditSeverity.WARNING


def test_check_finding_critical_codes() -> None:
    for code in (
        "numeric_grounding",
        "citation_provenance_closure",
        "claim_reference_closure",
        "evidence_reference_closure",
        "draft_section_integrity",
        "forbidden_investment_language",
        "internal_alias_leak",
    ):
        assert check_finding_severity(code) is AuditSeverity.CRITICAL, code


def test_check_finding_warning_codes() -> None:
    for code in ("outline_section_coverage", "conflict_gap_preservation", "empty_section"):
        assert check_finding_severity(code) is AuditSeverity.WARNING, code


def test_check_finding_unknown_code_conservative_critical() -> None:
    assert check_finding_severity("some_unknown_code") is AuditSeverity.CRITICAL


def test_audit_issue_critical_types() -> None:
    for issue_type in (
        "unsupported_by_evidence",
        "stale_or_temporally_misaligned",
        "evidence_mismatch",
        "claim_misrepresentation",
    ):
        assert audit_issue_severity(issue_type) is AuditSeverity.CRITICAL, issue_type


def test_audit_issue_warning_types() -> None:
    for issue_type in (
        "weak_source_quality",
        "omitted_counterevidence",
        "causal_overreach",
        "valuation_overreach",
        "insufficient_evidence",
        "unresolved_conflict",
    ):
        assert audit_issue_severity(issue_type) is AuditSeverity.WARNING, issue_type


def test_audit_issue_info_types() -> None:
    assert audit_issue_severity("wording_overclaim") is AuditSeverity.INFO


def test_audit_issue_unknown_conservative_critical() -> None:
    assert audit_issue_severity("some_new_type") is AuditSeverity.CRITICAL


def test_classify_report_no_findings_no_issues_info() -> None:
    sev = classify_report_severity(
        finding_codes=[], finding_section_ids=[], issues=[], degraded_section_ids=frozenset()
    )
    assert sev is AuditSeverity.INFO


def test_classify_report_warning_issue_overrides_pass_check() -> None:
    sev = classify_report_severity(
        finding_codes=[],
        finding_section_ids=[],
        issues=[FakeIssue("unresolved_conflict", "S1")],
        degraded_section_ids=frozenset(),
    )
    assert sev is AuditSeverity.WARNING


def test_classify_report_critical_wins_over_all() -> None:
    sev = classify_report_severity(
        finding_codes=["conflict_gap_preservation"],  # warning
        finding_section_ids=["S1"],
        issues=[FakeIssue("wording_overclaim", "S1")],  # info
        degraded_section_ids=frozenset(),
    )
    assert sev is AuditSeverity.WARNING
    sev2 = classify_report_severity(
        finding_codes=["citation_provenance_closure"],  # critical
        finding_section_ids=["S1"],
        issues=[],
        degraded_section_ids=frozenset(),
    )
    assert sev2 is AuditSeverity.CRITICAL


def test_classify_report_degraded_lowers_to_warning() -> None:
    sev = classify_report_severity(
        finding_codes=["citation_provenance_closure"],  # 本应 critical
        finding_section_ids=["S3"],
        issues=[FakeIssue("unsupported_by_evidence", "S3")],  # 本应 critical
        degraded_section_ids=frozenset({"S3"}),
    )
    assert sev is AuditSeverity.WARNING


def test_classify_report_degraded_non_degraded_mix_keeps_critical() -> None:
    sev = classify_report_severity(
        finding_codes=["numeric_grounding"],  # critical
        finding_section_ids=["S1"],  # non-degraded
        issues=[FakeIssue("wording_overclaim", "S3")],  # degraded → warning
        degraded_section_ids=frozenset({"S3"}),
    )
    assert sev is AuditSeverity.CRITICAL
