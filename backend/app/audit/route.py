"""Deterministic route derivation (stage 5D, spec O).

模型不决定 routing。程序根据 resolved issues 确定 status / route：

- **0 issues** → `status=pass`, `route=pass`；
- **有 issues** → `status=fail`；
- **route 优先级**：`human_review` > `research` > `rewrite`：
  - `human_review`：`unresolved_conflict` 且 severity `high` / `critical`；
  - `research`：`unsupported_by_evidence` / `weak_source_quality` /
    `stale_or_temporally_misaligned` / `insufficient_evidence`；
  - `rewrite`：`evidence_mismatch` / `claim_misrepresentation` /
    `wording_overclaim` / `omitted_counterevidence` / `causal_overreach` /
    `valuation_overreach` / normal `unresolved_conflict`。
- 多个 issues → 取**最高优先级** route（为 5E 提供稳定输入）。
"""

from app.audit.contracts import (
    AUDIT_ROUTE_HUMAN_REVIEW,
    AUDIT_ROUTE_PASS,
    AUDIT_ROUTE_RESEARCH,
    AUDIT_ROUTE_REWRITE,
    AUDIT_SEVERITY_CRITICAL,
    AUDIT_SEVERITY_HIGH,
    AUDIT_STATUS_FAIL,
    AUDIT_STATUS_PASS,
    ResolvedAuditIssue,
)

_HUMAN_REVIEW_ISSUE_TYPES = frozenset({"unresolved_conflict"})
_RESEARCH_ISSUE_TYPES = frozenset(
    {
        "unsupported_by_evidence",
        "weak_source_quality",
        "stale_or_temporally_misaligned",
        "insufficient_evidence",
    }
)
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

# 单一 issue → route 优先级（3=human_review > 2=research > 1=rewrite）。
_PRIORITY_HUMAN_REVIEW = 3
_PRIORITY_RESEARCH = 2
_PRIORITY_REWRITE = 1


def derive_route(issues: list[ResolvedAuditIssue]) -> tuple[str, str]:
    """返回 `(status, route)`。

    - 空 issues → `(pass, pass)`（0 model 判断参与，纯确定性）；
    - 非空 → `(fail, 最高优先级 route)`。
    """
    if not issues:
        return AUDIT_STATUS_PASS, AUDIT_ROUTE_PASS

    priority = max(_issue_route_priority(issue) for issue in issues)
    if priority >= _PRIORITY_HUMAN_REVIEW:
        return AUDIT_STATUS_FAIL, AUDIT_ROUTE_HUMAN_REVIEW
    if priority == _PRIORITY_RESEARCH:
        return AUDIT_STATUS_FAIL, AUDIT_ROUTE_RESEARCH
    return AUDIT_STATUS_FAIL, AUDIT_ROUTE_REWRITE


def _issue_route_priority(issue: ResolvedAuditIssue) -> int:
    """single issue → route 优先级（3 / 2 / 1）。"""
    if issue.issue_type in _HUMAN_REVIEW_ISSUE_TYPES and issue.severity in (
        AUDIT_SEVERITY_HIGH,
        AUDIT_SEVERITY_CRITICAL,
    ):
        return _PRIORITY_HUMAN_REVIEW
    if issue.issue_type in _RESEARCH_ISSUE_TYPES:
        return _PRIORITY_RESEARCH
    # rewrite 集合 + normal unresolved_conflict；未覆盖类型（防御性兜底）也走 rewrite。
    return _PRIORITY_REWRITE
