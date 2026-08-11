"""derive_route 纯函数单测（stage 5D，spec O）：0 DB / 0 LLM。

确定性 route 派生（模型不决定 routing）：
- 0 issues → (pass, pass)；
- 有 issues → (fail, 最高优先级 route)；
- 优先级：human_review（unresolved_conflict high/critical）> research
  （unsupported_by_evidence / weak_source_quality /
  stale_or_temporally_misaligned / insufficient_evidence）> rewrite
  （evidence_mismatch / claim_misrepresentation / wording_overclaim /
  omitted_counterevidence / causal_overreach / valuation_overreach /
  normal unresolved_conflict）。
"""

from app.audit.contracts import ResolvedAuditIssue
from app.audit.route import derive_route


def _issue(issue_type: str, severity: str = "normal") -> ResolvedAuditIssue:
    return ResolvedAuditIssue(
        issue_type=issue_type,
        severity=severity,
        section_id="sec_a",
        paragraph_index=0,
        message="测试 issue",
        related_claim_ids=(),
        related_evidence_card_ids=(),
    )


def test_route_pass() -> None:
    assert derive_route([]) == ("pass", "pass")


def test_route_rewrite() -> None:
    assert derive_route([_issue("wording_overclaim")]) == ("fail", "rewrite")
    assert derive_route([_issue("evidence_mismatch")]) == ("fail", "rewrite")
    assert derive_route([_issue("omitted_counterevidence")]) == ("fail", "rewrite")
    assert derive_route([_issue("unresolved_conflict")]) == ("fail", "rewrite")


def test_route_research() -> None:
    assert derive_route([_issue("unsupported_by_evidence")]) == ("fail", "research")
    assert derive_route([_issue("weak_source_quality")]) == ("fail", "research")
    assert derive_route([_issue("insufficient_evidence")]) == ("fail", "research")


def test_route_human_review() -> None:
    assert derive_route([_issue("unresolved_conflict", "high")]) == ("fail", "human_review")
    assert derive_route([_issue("unresolved_conflict", "critical")]) == ("fail", "human_review")


def test_route_priority() -> None:
    # human_review > research > rewrite；multi issue 取最高优先级。
    assert derive_route([_issue("wording_overclaim"), _issue("unsupported_by_evidence")]) == (
        "fail",
        "research",
    )
    assert derive_route([_issue("wording_overclaim"), _issue("unresolved_conflict", "high")]) == (
        "fail",
        "human_review",
    )
