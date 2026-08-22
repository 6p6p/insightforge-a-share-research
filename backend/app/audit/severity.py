"""AuditSeverity —— 研究审核风险的确定性分级（v1.2.5 风险提示系统）。

v1.2.5 核心转变：**审核发现问题 ≠ 报告不可交付**。系统负责发现并解释风险，
最终接受权交给用户。本模块只做确定性分类与提醒分级，**不再输出阻断决策**：

- `critical`（CRITICAL_ALERT 严重审核提醒）：未来证据 / 时间对齐违规 / 数据
  真实性无法确认 / 数字接地失败 / 溯源断裂 / 完整性失败——**不阻断接受**，
  仅提示「需要用户重点关注」；用户仍可接受报告 / 再次补充研究 / 取消研究；
- `warning`（WARNING 审核提醒）：numeric_grounding / conflict_gap /
  evidence coverage 不足 / model_unavailable / draft_quality_guard /
  degraded section / empty section / risks_and_gaps 缺口——允许接受；
- `info`（INFO 提示）：措辞建议 / 可选上下文——正常完成。

S6/S7（risks_and_gaps 风险、冲突与证据缺口章节）天然包含不确定性 / 数据缺口
/ 冲突分析 / 风险提示——其 numeric_grounding / conflict_gap / missing evidence
一律按 WARNING / SECTION_WARNING 处理，绝不上浮为 REPORT_BLOCKING。

`AuditImpactScope` 枚举值保留（兼容持久化与既有序列化），但 `REPORT_BLOCKING`
语义从「阻断接受」改为「CRITICAL_ALERT 严重提醒」——`accepts_with_scope` 恒为
True（所有 scope 均允许人工接受，仅决定 completed vs completed_with_warnings）。

`Stage5ApproveRequiresPassCheck` 不再由本模块触发；它只在系统级不可恢复错误
（状态损坏 / artifact 不存在 / 数据库一致性破坏）由 finalize_on_approve 抛出。

本模块不持有任何 DB / LLM 依赖，可被 finalize_on_approve 与 backflow
acceptance 守卫复用。
"""

from enum import StrEnum

from app.report_outline.contracts import SECTION_TYPE_RISKS_AND_GAPS


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
# 与 severity 正交。v1.2.5 起 scope 只决定提醒级别与完成状态，**不再阻断接受**：
# - REPORT_BLOCKING（枚举值保留）→ 产品语义 CRITICAL_ALERT「严重审核提醒」：
#   不阻断接受，接受后 completed_with_warnings，仅在 UI 强调「需要重点关注」；
# - SECTION_WARNING / SECTION_UNAVAILABLE → WARNING「审核提醒」，允许接受
#   （带提醒）→ completed_with_warnings；
# - INFO → 无提醒 → completed。
class AuditImpactScope(StrEnum):
    REPORT_BLOCKING = "report_blocking"        # CRITICAL_ALERT：严重审核提醒（不阻断）
    SECTION_WARNING = "section_warning"        # WARNING：章节级质量提醒（允许接受）
    SECTION_UNAVAILABLE = "section_unavailable"  # WARNING：章节不可用/降级（允许接受）
    INFO = "info"                              # 无影响（正常完成）


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


# S6/S7（risks_and_gaps）特例：风险 / 冲突 / 证据缺口章节天然包含不确定性、
# 数据缺口、冲突分析与风险提示——其 numeric grounding / conflict gap / missing
# evidence 一律按 SECTION_WARNING（WARNING 提醒），绝不上浮为 REPORT_BLOCKING。
def is_risks_and_gaps_section(section_type: str | None) -> bool:
    return section_type == SECTION_TYPE_RISKS_AND_GAPS


_S6_S7_DOWNGRADE_CODES = frozenset(
    {
        "numeric_grounding",  # 数字口径差异/无法确认 → 数据口径风险提示
        "conflict_gap_preservation",  # 冲突/缺口讨论 → 风险章节天然内容
    }
)


def check_finding_scope(code: str, section_type: str | None = None) -> AuditImpactScope:
    """单条 Check finding code -> impact scope（确定性；未知 code 保守 -> REPORT_BLOCKING）。

    v1.2.5：REPORT_BLOCKING 语义为「严重审核提醒（CRITICAL_ALERT）」而非阻断；
    S6/S7（risks_and_gaps）章节的 numeric / conflict / missing 强制 SECTION_WARNING。
    """
    if section_type is not None and is_risks_and_gaps_section(section_type):
        if code in _S6_S7_DOWNGRADE_CODES:
            return AuditImpactScope.SECTION_WARNING
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


_S6_S7_DOWNGRADE_ISSUE_TYPES = frozenset(
    {
        "unsupported_by_evidence",  # 数据真实性无法确认（S6/S7 中属证据缺口提示）
        "insufficient_evidence",  # 证据不足（S6/S7 天然内容）
        "unresolved_conflict",  # 冲突未解决（S6/S7 天然内容）
        "omitted_counterevidence",  # 反证缺失（S6/S7 天然内容）
    }
)


def audit_issue_scope(
    issue_type: str, section_type: str | None = None
) -> AuditImpactScope:
    """单条 Audit issue_type -> impact scope（确定性；未知类型保守 -> REPORT_BLOCKING）。

    v1.2.5：S6/S7（risks_and_gaps）章节的 numeric / conflict / missing evidence
    一律 SECTION_WARNING（不 REPORT_BLOCKING）。
    """
    if section_type is not None and is_risks_and_gaps_section(section_type):
        if issue_type in _S6_S7_DOWNGRADE_ISSUE_TYPES:
            return AuditImpactScope.SECTION_WARNING
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
    section_type_by_id: dict[str, str] | None = None,
) -> AuditImpactScope:
    """Check findings 汇总 -> report 影响范围（degraded 章节 -> SECTION_UNAVAILABLE）。

    v1.2.5：section_type_by_id（section_id -> section_type）用于 S6/S7 特例
    （risks_and_gaps 的 numeric/conflict/missing 降级 SECTION_WARNING）。
    """
    overall = AuditImpactScope.INFO
    for code, section_id in zip(finding_codes, finding_section_ids, strict=False):
        if section_id in degraded_section_ids:
            item = AuditImpactScope.SECTION_UNAVAILABLE
        else:
            st = (section_type_by_id or {}).get(section_id) if section_id else None
            item = check_finding_scope(code, section_type=st)
        overall = stricter_scope(overall, item)
    return overall


def classify_issue_scope(
    issues: list[object],
    degraded_section_ids: frozenset[str],
    section_type_by_id: dict[str, str] | None = None,
) -> AuditImpactScope:
    """Audit issues 汇总 -> report 影响范围（degraded 章节 -> SECTION_UNAVAILABLE）。"""
    overall = AuditImpactScope.INFO
    for issue in issues:
        section_id = getattr(issue, "section_id", None)
        if section_id in degraded_section_ids:
            item = AuditImpactScope.SECTION_UNAVAILABLE
        else:
            st = (section_type_by_id or {}).get(section_id) if section_id else None
            item = audit_issue_scope(getattr(issue, "issue_type", ""), section_type=st)
        overall = stricter_scope(overall, item)
    return overall


def classify_report_scope(
    finding_codes: list[str],
    finding_section_ids: list[str],
    issues: list[object],
    degraded_section_ids: frozenset[str],
    section_type_by_id: dict[str, str] | None = None,
) -> AuditImpactScope:
    """Check findings + Audit issues 联合影响范围（REPORT_BLOCKING 最高）。"""
    return stricter_scope(
        classify_check_scope(
            finding_codes, finding_section_ids, degraded_section_ids, section_type_by_id
        ),
        classify_issue_scope(issues, degraded_section_ids, section_type_by_id),
    )


def accepts_with_scope(scope: AuditImpactScope) -> bool:
    """v1.2.5：任何 scope 均允许人工接受（审核发现问题 ≠ 报告不可交付）。

    scope 只决定完成状态（INFO→completed；其他→completed_with_warnings）
    与提醒级别（REPORT_BLOCKING→CRITICAL_ALERT 严重提醒），不参与阻断。
    """
    return True



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
    section_type_by_id: dict[str, str] | None = None,
) -> AuditSeverity:
    """Check findings 汇总 report 接受严重度（取最严格）。"""
    overall = AuditSeverity.INFO
    for code, section_id in zip(finding_codes, finding_section_ids, strict=False):
        if section_id in degraded_section_ids:
            # degraded 占位 findings 一律 WARNING（v1.2.2 兼容），不升级 critical。
            item = _DEGRADED_SECTION_SEVERITY
        else:
            st = (section_type_by_id or {}).get(section_id) if section_id else None
            item = _finding_severity_for_section(code, st)
        overall = stricter(overall, item)
    return overall


# S6/S7 风险章节的 severity 一律最多 WARNING（不 CRITICAL）。
def _issue_severity_for_section(issue_type: str, section_type: str | None) -> AuditSeverity:
    if section_type is not None and is_risks_and_gaps_section(section_type):
        if issue_type in _S6_S7_DOWNGRADE_ISSUE_TYPES:
            return AuditSeverity.WARNING
    return audit_issue_severity(issue_type)


def _finding_severity_for_section(code: str, section_type: str | None) -> AuditSeverity:
    if section_type is not None and is_risks_and_gaps_section(section_type):
        if code in _S6_S7_DOWNGRADE_CODES:
            return AuditSeverity.WARNING
    return check_finding_severity(code)


def classify_issue_severity(
    issues: list[object],
    degraded_section_ids: frozenset[str],
    section_type_by_id: dict[str, str] | None = None,
) -> AuditSeverity:
    """Audit issues 汇总 -> 接受严重度（degraded 章节一律 warning）。"""
    overall = AuditSeverity.INFO
    for issue in issues:
        section_id = getattr(issue, "section_id", None)
        if section_id in degraded_section_ids:
            item = AuditSeverity.WARNING
        else:
            st = (section_type_by_id or {}).get(section_id) if section_id else None
            item = _issue_severity_for_section(getattr(issue, "issue_type", ""), st)
        overall = stricter(overall, item)
    return overall


def classify_report_severity(
    finding_codes: list[str],
    finding_section_ids: list[str],
    issues: list[object],
    degraded_section_ids: frozenset[str],
    section_type_by_id: dict[str, str] | None = None,
) -> AuditSeverity:
    """Check findings + Audit issues 联合接受严重度（取最严格）。"""
    check_sev = classify_check_severity(
        finding_codes, degraded_section_ids, finding_section_ids, section_type_by_id
    )
    issue_sev = classify_issue_severity(issues, degraded_section_ids, section_type_by_id)
    return stricter(check_sev, issue_sev)


# ---------------------------------------------------------------- 产品语义（v1.2.5）

# severity/scope -> 产品提醒等级（前端展示用；不参与阻断）。
# CRITICAL_ALERT = 「重要审核提醒（需要重点关注）」；WARNING = 「审核提醒」。
_PRODUCT_LEVEL_BY_SCOPE = {
    AuditImpactScope.REPORT_BLOCKING: "CRITICAL_ALERT",
    AuditImpactScope.SECTION_WARNING: "WARNING",
    AuditImpactScope.SECTION_UNAVAILABLE: "WARNING",
    AuditImpactScope.INFO: "INFO",
}

_PRODUCT_LEVEL_BY_SEVERITY = {
    AuditSeverity.CRITICAL: "CRITICAL_ALERT",
    AuditSeverity.WARNING: "WARNING",
    AuditSeverity.INFO: "INFO",
}


def product_level_of_scope(scope: AuditImpactScope) -> str:
    """impact scope -> 产品提醒等级（CRITICAL_ALERT / WARNING / INFO）。"""
    return _PRODUCT_LEVEL_BY_SCOPE.get(scope, AuditImpactScope.INFO.value)


def product_level_of_severity(severity: AuditSeverity) -> str:
    """severity -> 产品提醒等级。"""
    return _PRODUCT_LEVEL_BY_SEVERITY.get(severity, AuditSeverity.INFO.value)

