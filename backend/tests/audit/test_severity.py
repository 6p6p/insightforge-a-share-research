"""AuditSeverity 确定性分类单元测试（v1.2.3 §2/§3）。

验证：LLM 不参与 —— severity 完全由 issue_type / check finding code + degraded
属性确定性推导；保守映射（未知 → critical；degraded → warning；绝不降级 critical）。
纯函数，零 DB / 零网络。
"""

from app.audit.severity import (
    AuditImpactScope,
    AuditSeverity,
    accepts_with_scope,
    audit_issue_scope,
    audit_issue_severity,
    check_finding_scope,
    check_finding_severity,
    classify_check_scope,
    classify_issue_scope,
    classify_report_scope,
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




# ================================================================ v1.2.4 impact scope

def test_scope_ranks_and_stricter() -> None:
    from app.audit.severity import impact_scope_rank, stricter_scope

    assert impact_scope_rank(AuditImpactScope.INFO) < impact_scope_rank(
        AuditImpactScope.SECTION_UNAVAILABLE
    )
    assert impact_scope_rank(AuditImpactScope.SECTION_UNAVAILABLE) < impact_scope_rank(
        AuditImpactScope.SECTION_WARNING
    )
    assert impact_scope_rank(AuditImpactScope.SECTION_WARNING) < impact_scope_rank(
        AuditImpactScope.REPORT_BLOCKING
    )
    # stricter_scope 取更严
    assert (
        stricter_scope(AuditImpactScope.INFO, AuditImpactScope.REPORT_BLOCKING)
        is AuditImpactScope.REPORT_BLOCKING
    )
    assert (
        stricter_scope(
            AuditImpactScope.SECTION_WARNING, AuditImpactScope.SECTION_UNAVAILABLE
        )
        is AuditImpactScope.SECTION_WARNING
    )


def test_scope_accepts() -> None:
    # v1.2.5：任何 scope 均允许人工接受（审核发现问题 ≠ 报告不可交付）。
    assert accepts_with_scope(AuditImpactScope.REPORT_BLOCKING)
    assert accepts_with_scope(AuditImpactScope.SECTION_WARNING)
    assert accepts_with_scope(AuditImpactScope.SECTION_UNAVAILABLE)
    assert accepts_with_scope(AuditImpactScope.INFO)


def test_check_finding_scope_report_codes() -> None:
    for code in (
        "numeric_grounding",
        "citation_provenance_closure",
        "claim_reference_closure",
        "evidence_reference_closure",
        "forbidden_investment_language",
    ):
        assert check_finding_scope(code) is AuditImpactScope.REPORT_BLOCKING, code


def test_check_finding_scope_section_codes() -> None:
    for code in (
        "outline_section_coverage",
        "draft_section_integrity",
        "conflict_gap_preservation",
        "empty_section",
        "internal_alias_leak",
    ):
        assert check_finding_scope(code) is AuditImpactScope.SECTION_WARNING, code


def test_check_finding_scope_unknown_conservative_blocking() -> None:
    # 未知 code → 保守 REPORT_BLOCKING（绝不悄悄放行）
    assert check_finding_scope("some_unknown_code") is AuditImpactScope.REPORT_BLOCKING


def test_audit_issue_scope_report_types() -> None:
    for t in (
        "unsupported_by_evidence",
        "stale_or_temporally_misaligned",
        "evidence_mismatch",
        "claim_misrepresentation",
    ):
        assert audit_issue_scope(t) is AuditImpactScope.REPORT_BLOCKING, t


def test_audit_issue_scope_section_types() -> None:
    for t in (
        "weak_source_quality",
        "omitted_counterevidence",
        "causal_overreach",
        "valuation_overreach",
        "insufficient_evidence",
        "unresolved_conflict",
    ):
        assert audit_issue_scope(t) is AuditImpactScope.SECTION_WARNING, t


def test_audit_issue_scope_info_types() -> None:
    assert audit_issue_scope("wording_overclaim") is AuditImpactScope.INFO


def test_audit_issue_scope_unknown_conservative() -> None:
    assert audit_issue_scope("unknown_type") is AuditImpactScope.REPORT_BLOCKING


def test_scope_section_issue_allows_accept() -> None:
    # §6(1) S5/S6 风险章节缺失 / model_unavailable → section 级 → 允许接受
    scope = classify_report_scope(
        finding_codes=["outline_section_coverage"],  # S5 未生成
        finding_section_ids=["S5"],
        issues=[],
        degraded_section_ids=frozenset(),
    )
    assert scope is AuditImpactScope.SECTION_WARNING
    assert accepts_with_scope(scope)


def test_scope_degraded_section_unavailable_allows_accept() -> None:
    # model_unavailable → degraded section → SECTION_UNAVAILABLE → 允许接受
    scope = classify_report_scope(
        finding_codes=["empty_section"],
        finding_section_ids=["S3"],
        issues=[FakeIssue("insufficient_evidence", "S3")],
        degraded_section_ids=frozenset({"S3"}),
    )
    assert scope is AuditImpactScope.SECTION_UNAVAILABLE
    assert accepts_with_scope(scope)


def test_scope_numeric_grounding_critical_alert_not_blocking() -> None:
    # v1.2.5：numeric grounding → REPORT_BLOCKING 枚举值（CRITICAL_ALERT 严重
    # 提醒），但接受不被阻断——用户仍可接受/补充研究/取消。
    scope = classify_report_scope(
        finding_codes=["numeric_grounding"],
        finding_section_ids=["S1"],
        issues=[],
        degraded_section_ids=frozenset(),
    )
    assert scope is AuditImpactScope.REPORT_BLOCKING
    assert accepts_with_scope(scope)


def test_scope_future_evidence_critical_alert_not_blocking() -> None:
    # v1.2.5：未来证据 / temporal violation → REPORT_BLOCKING 枚举值（严重提醒），
    # 接受不被阻断。
    scope = classify_report_scope(
        finding_codes=[],
        finding_section_ids=[],
        issues=[FakeIssue("stale_or_temporally_misaligned", "S1")],
        degraded_section_ids=frozenset(),
    )
    assert scope is AuditImpactScope.REPORT_BLOCKING
    assert accepts_with_scope(scope)


def test_scope_s6_s7_numeric_grounding_section_warning() -> None:
    # v1.2.5 §5：S6/S7（risks_and_gaps）章节的 numeric grounding → SECTION_WARNING
    # （不 REPORT_BLOCKING）。
    scope = classify_report_scope(
        finding_codes=["numeric_grounding"],
        finding_section_ids=["S6"],
        issues=[],
        degraded_section_ids=frozenset(),
        section_type_by_id={"S6": "risks_and_gaps"},
    )
    assert scope is AuditImpactScope.SECTION_WARNING


def test_scope_risk_section_insufficient_evidence_warning() -> None:
    # v1.2.5 §5：S6/S7 的 missing evidence issue → SECTION_WARNING。
    scope = classify_report_scope(
        finding_codes=[],
        finding_section_ids=[],
        issues=[FakeIssue("insufficient_evidence", "S7")],
        degraded_section_ids=frozenset(),
        section_type_by_id={"S7": "risks_and_gaps"},
    )
    assert scope is AuditImpactScope.SECTION_WARNING


def test_scope_unknown_issue_conservative_blocking() -> None:
    # §6(4) 未知 issue type → 保守 REPORT_BLOCKING
    scope = classify_report_scope(
        finding_codes=[],
        finding_section_ids=[],
        issues=[FakeIssue("some_unknown_type", "S1")],
        degraded_section_ids=frozenset(),
    )
    assert scope is AuditImpactScope.REPORT_BLOCKING


def test_scope_report_beats_section() -> None:
    # 一节 warning + 一节 report-blocking → REPORT_BLOCKING
    scope = classify_report_scope(
        finding_codes=["outline_section_coverage", "numeric_grounding"],
        finding_section_ids=["S5", "S1"],
        issues=[FakeIssue("unresolved_conflict", "S2")],
        degraded_section_ids=frozenset({"S3"}),
    )
    assert scope is AuditImpactScope.REPORT_BLOCKING


def test_scope_no_findings_info() -> None:
    scope = classify_report_scope(
        finding_codes=[],
        finding_section_ids=[],
        issues=[],
        degraded_section_ids=frozenset(),
    )
    assert scope is AuditImpactScope.INFO


def test_scope_degraded_report_critical_stays_non_blocking() -> None:
    # v1.2.2-A 兼容：degraded 占位章节（model_unavailable 诚实占位，报告无数据/引文）
    # findings 一律 SECTION_UNAVAILABLE，不阻断接受（与 v1.2.3 degraded->warning 一致）。
    scope = classify_report_scope(
        finding_codes=["numeric_grounding"],
        finding_section_ids=["S3"],
        issues=[],
        degraded_section_ids=frozenset({"S3"}),
    )
    assert scope is AuditImpactScope.SECTION_UNAVAILABLE
    assert accepts_with_scope(scope)


def test_classify_check_scope_and_issue_scope_union() -> None:
    assert classify_check_scope([], [], frozenset()) is AuditImpactScope.INFO
    assert classify_issue_scope([], frozenset()) is AuditImpactScope.INFO
    assert classify_check_scope(
        ["empty_section"], ["S2"], frozenset()
    ) is AuditImpactScope.SECTION_WARNING
    assert classify_issue_scope(
        [FakeIssue("unsupported_by_evidence", "S1")], frozenset()
    ) is AuditImpactScope.REPORT_BLOCKING
