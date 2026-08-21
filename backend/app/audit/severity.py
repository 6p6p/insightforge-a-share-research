"""AuditSeverity —— 人工接受决策的确定性严重度分级（v1.2.3）。

评价对象的 severity 由**程序确定性分类**，不在 prompt 让 LLM 自由裁决：

- `critical`（阻断接受）：未来证据 / 时间对齐违规 / 引用或溯源失败 / 证据溯源
  断裂 / 数字接地失败 / 数据真实性无法确认 / 确定性完整性失败；
- `warning`（允许人工接受 → completed_with_warnings）：draft_quality_guard /
  model_unavailable / degraded section / 显式标记的不完整章节 / conflict_gap
  讨论不足 / 非关键分析缺失；
- `info`（提示，不阻断）：措辞建议 / 可选上下文。

保守策略：无法准确映射的类型按阻断方向处理（宁可阻断，不可放行），绝不
critical -> warning 降级。

本模块不持有任何 DB / LLM 依赖，可被 finalize_on_approve 与 backflow
acceptance 守卫复用。
"""

from enum import StrEnum


class AuditSeverity(StrEnum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


def severity_rank(severity: AuditSeverity) -> int:
    return {
        AuditSeverity.INFO: 1,
        AuditSeverity.WARNING: 2,
        AuditSeverity.CRITICAL: 3,
    }[severity]


def stricter(a: AuditSeverity, b: AuditSeverity) -> AuditSeverity:
    return a if severity_rank(a) >= severity_rank(b) else b


# ---------------------------------------------------------------- check finding

# deterministic Check finding code → severity（app/report/checks.py 同步维护）。
_CHECK_CRITICAL_CODES = frozenset(
    {
        "numeric_grounding",  # 数字接地失败（无法追溯）
        "citation_provenance_closure",  # 引文溯源断裂（invalid reference）
        "claim_reference_closure",  # claim 引用闭环失败
        "evidence_reference_closure",  # evidence 引用闭环失败
        "draft_section_integrity",  # 草稿完整性（identity mismatch）
        "forbidden_investment_language",  # 合规红线（含投资建议）
        "internal_alias_leak",  # 内部标识泄漏（溯源线）
    }
)

_CHECK_WARNING_CODES = frozenset(
    {
        "outline_section_coverage",  # 非关键章节缺失
        "conflict_gap_preservation",  # conflict/gap 讨论不足
        "empty_section",  # 章节为空（显式标记的不完整）
    }
)

# degraded 章节产生的 findings 一律视为 warning（v1.2.2 兼容：degraded
# 占位不因内容本身阻断人工批准；与既有 "全部可归因时允许带警告完成" 对齐）。
_DEGRADED_SECTION_SEVERITY = AuditSeverity.WARNING


def check_finding_severity(code: str) -> AuditSeverity:
    """单条 Check finding code → severity（确定性，未知 code 保守 -> critical）。"""
    if code in _CHECK_CRITICAL_CODES:
        return AuditSeverity.CRITICAL
    if code in _CHECK_WARNING_CODES:
        return AuditSeverity.WARNING
    return AuditSeverity.CRITICAL


# ---------------------------------------------------------------- audit issue

# Audit issue_type → severity（确定性；LLM 只产出 issue_type + message，不裁决
# 接受级别）。unresolved_conflict 按现有路由阈值语言保留。
_AUDIT_ISSUE_CRITICAL_TYPES = frozenset(
    {
        "unsupported_by_evidence",  # 数据真实性无法确认
        "stale_or_temporally_misaligned",  # 未来证据 / 时间对齐违规
        "evidence_mismatch",  # 证据错配（provenance）
        "claim_misrepresentation",  # 主张被歪曲（provenance）
    }
)

_AUDIT_ISSUE_WARNING_TYPES = frozenset(
    {
        "weak_source_quality",  # 非关键证据质量
        "omitted_counterevidence",  # 反证缺失（非关键）
        "causal_overreach",  # 因果过度推断
        "valuation_overreach",  # 估值过度推断
        "insufficient_evidence",  # 证据不足（非关键）
        "unresolved_conflict",  # conflict_gap 讨论不足（允许接受）
    }
)

_AUDIT_ISSUE_INFO_TYPES = frozenset(
    {
        "wording_overclaim",  # 措辞建议
    }
)


def audit_issue_severity(issue_type: str) -> AuditSeverity:
    """单条 Audit issue_type -> severity（确定性，未知类型保守 -> critical）。"""
    if issue_type in _AUDIT_ISSUE_CRITICAL_TYPES:
        return AuditSeverity.CRITICAL
    if issue_type in _AUDIT_ISSUE_WARNING_TYPES:
        return AuditSeverity.WARNING
    if issue_type in _AUDIT_ISSUE_INFO_TYPES:
        return AuditSeverity.INFO
    return AuditSeverity.CRITICAL


# ---------------------------------------------------------------- report level


def classify_check_severity(
    finding_codes: list[str],
    degraded_section_ids: frozenset[str],
    finding_section_ids: list[str],
) -> AuditSeverity:
    """Check findings 汇总 report 接受严重度（取最严格）。"""
    overall = AuditSeverity.INFO
    for code, section_id in zip(finding_codes, finding_section_ids, strict=False):
        if section_id in degraded_section_ids:
            # degraded 占位 findings 一律 warning（v1.2.2 兼容），不升级 critical。
            item = _DEGRADED_SECTION_SEVERITY
        else:
            item = check_finding_severity(code)
        overall = stricter(overall, item)
    return overall


def classify_issue_severity(
    issues: list[object],
    degraded_section_ids: frozenset[str],
) -> AuditSeverity:
    """Audit issues 汇总 -> 接受严重度（degraded 章节一律 warning）。"""
    overall = AuditSeverity.INFO
    for issue in issues:
        section_id = getattr(issue, "section_id", None)
        if section_id in degraded_section_ids:
            item = AuditSeverity.WARNING
        else:
            item = audit_issue_severity(getattr(issue, "issue_type", ""))
        overall = stricter(overall, item)
    return overall


def classify_report_severity(
    finding_codes: list[str],
    finding_section_ids: list[str],
    issues: list[object],
    degraded_section_ids: frozenset[str],
) -> AuditSeverity:
    """Check findings + Audit issues 联合接受严重度（取最严格）。"""
    check_sev = classify_check_severity(finding_codes, degraded_section_ids, finding_section_ids)
    issue_sev = classify_issue_severity(issues, degraded_section_ids)
    return stricter(check_sev, issue_sev)
