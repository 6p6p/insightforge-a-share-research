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


# ================================================================ impact scope (v1.2.4)

# 影响范围维度：**一个 finding/issue 属于 REPORT 级还是 SECTION 级** 是确定性判定，
# 与 severity 正交。section 级缺陷（某章节缺失/降级/质量不足）不应阻断整个报告的
# 人工接受，只标记提醒 -> completed_with_warnings；只有 REPORT 级（破坏整体可信度）
# 才阻断接受。
class AuditImpactScope(StrEnum):
    REPORT_BLOCKING = "report_blocking"        # 整体可信度破坏 -> 阻断接受
    SECTION_WARNING = "section_warning"        # 章节级质量提醒 -> 允许接受（带提醒）
    SECTION_UNAVAILABLE = "section_unavailable"  # 章节不可用/降级 -> 允许接受
    INFO = "info"                              # 无影响


def impact_scope_rank(scope: AuditImpactScope) -> int:
    return {
        AuditImpactScope.INFO: 1,
        AuditImpactScope.SECTION_UNAVAILABLE: 2,
        AuditImpactScope.SECTION_WARNING: 3,
        AuditImpactScope.REPORT_BLOCKING: 4,
    }[scope]


def stricter_scope(a: AuditImpactScope, b: AuditImpactScope) -> AuditImpactScope:
    """worst-of 取更严（REPORT_BLOCKING 最高；接受决策只看是否 REPORT_BLOCKING）。"""
    return a if impact_scope_rank(a) >= impact_scope_rank(b) else b


# deterministic Check finding code -> impact scope（app/report/checks.py 同步维护）。
# 只有破坏整体可信度的 data-truth 类 code 属于 REPORT；章节装配/覆盖/空/冲突提醒类
# 属 SECTION（允许接受）。
_CHECK_REPORT_BLOCKING_CODES = frozenset(
    {
        "numeric_grounding",  # 数字接地失败（关键财务事实无法确认）
        "citation_provenance_closure",  # 引文溯源断裂（fake citation/provenance）
        "claim_reference_closure",  # claim 引用闭环失败
        "evidence_reference_closure",  # evidence 引用闭环失败
        "forbidden_investment_language",  # 合规红线（含投资建议）
    }
)

_CHECK_SECTION_CODES = frozenset(
    {
        "draft_section_integrity",  # 章节装配完整性（仅影响该章节）
        "outline_section_coverage",  # S5/S6 等风险章节未生成/缺失
        "conflict_gap_preservation",  # conflict/gap 讨论不足
        "empty_section",  # 非核心章节缺少分析内容
        "internal_alias_leak",  # 章节正文内的内部标识显示问题
    }
)


def check_finding_scope(code: str) -> AuditImpactScope:
    """单条 Check finding code -> impact scope（确定性；未知 code 保守 -> REPORT_BLOCKING）。"""
    if code in _CHECK_REPORT_BLOCKING_CODES:
        return AuditImpactScope.REPORT_BLOCKING
    if code in _CHECK_SECTION_CODES:
        return AuditImpactScope.SECTION_WARNING
    return AuditImpactScope.REPORT_BLOCKING


# Audit issue_type -> impact scope（确定性；与 severity 类型映射同源）。
_AUDIT_ISSUE_REPORT_BLOCKING_TYPES = frozenset(
    {
        "unsupported_by_evidence",  # 数据真实性无法确认
        "stale_or_temporally_misaligned",  # 未来证据 / 时间穿越
        "evidence_mismatch",  # 证据错配（provenance）
        "claim_misrepresentation",  # 主张被歪曲（provenance）
    }
)

_AUDIT_ISSUE_SECTION_TYPES = frozenset(
    {
        "weak_source_quality",  # 该章节证据质量
        "omitted_counterevidence",  # 该章节反证缺失
        "causal_overreach",  # 该章节因果过度推断
        "valuation_overreach",  # 该章节估值过度推断
        "insufficient_evidence",  # 该章节证据不足
        "unresolved_conflict",  # conflict_gap 讨论不足
    }
)

_AUDIT_ISSUE_INFO_TYPES = frozenset(
    {
        "wording_overclaim",  # 措辞建议
    }
)


def audit_issue_scope(issue_type: str) -> AuditImpactScope:
    """单条 Audit issue_type -> impact scope（确定性；未知类型保守 -> REPORT_BLOCKING）。"""
    if issue_type in _AUDIT_ISSUE_REPORT_BLOCKING_TYPES:
        return AuditImpactScope.REPORT_BLOCKING
    if issue_type in _AUDIT_ISSUE_SECTION_TYPES:
        return AuditImpactScope.SECTION_WARNING
    if issue_type in _AUDIT_ISSUE_INFO_TYPES:
        return AuditImpactScope.INFO
    return AuditImpactScope.REPORT_BLOCKING


# ------------------------------------------------- report-level scope


def classify_check_scope(
    finding_codes: list[str],
    finding_section_ids: list[str],
    degraded_section_ids: frozenset[str],
) -> AuditImpactScope:
    """Check findings 汇总 -> report 影响范围（degraded 章节 -> SECTION_UNAVAILABLE）。"""
    overall = AuditImpactScope.INFO
    for code, section_id in zip(finding_codes, finding_section_ids, strict=False):
        if section_id in degraded_section_ids:
            item = AuditImpactScope.SECTION_UNAVAILABLE
        else:
            item = check_finding_scope(code)
        overall = stricter_scope(overall, item)
    return overall


def classify_issue_scope(
    issues: list[object],
    degraded_section_ids: frozenset[str],
) -> AuditImpactScope:
    """Audit issues 汇总 -> report 影响范围（degraded 章节 -> SECTION_UNAVAILABLE）。"""
    overall = AuditImpactScope.INFO
    for issue in issues:
        section_id = getattr(issue, "section_id", None)
        if section_id in degraded_section_ids:
            item = AuditImpactScope.SECTION_UNAVAILABLE
        else:
            item = audit_issue_scope(getattr(issue, "issue_type", ""))
        overall = stricter_scope(overall, item)
    return overall


def classify_report_scope(
    finding_codes: list[str],
    finding_section_ids: list[str],
    issues: list[object],
    degraded_section_ids: frozenset[str],
) -> AuditImpactScope:
    """Check findings + Audit issues 联合影响范围（REPORT_BLOCKING 最高）。"""
    return stricter_scope(
        classify_check_scope(finding_codes, finding_section_ids, degraded_section_ids),
        classify_issue_scope(issues, degraded_section_ids),
    )


def accepts_with_scope(scope: AuditImpactScope) -> bool:
    """允许人工接受：REPORT_BLOCKING 阻断；SECTION_* / INFO 允许。"""
    return scope is not AuditImpactScope.REPORT_BLOCKING



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
