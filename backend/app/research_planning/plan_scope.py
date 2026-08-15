"""Research plan module-scope enforcement (V1.1 final closure).

Planner 输出必须**尊重用户选择的研究模块**（`ResearchTask.modules`）：

- `allowed_planner_modules(selected)`：用户 ResearchModule 值 → 允许的
  planner `AnalysisModule` 值（company_profile/business/events →
  business_event；financial → financial；macro → macro；risk → risk）；
- `allowed_scopes(selected)`：用户模块 → 允许的 `ResearchScope` 值；
- `apply_selected_modules(payload, selected, include_relative_valuation)`：
  纯函数过滤 plan payload：
  - analysis_modules ∩ 允许集合；research_scope ∩ 允许集合；
  - macro_needs 只在 MACRO 允许时保留；financial_needs 只在 FINANCIAL
    允许时保留；event_needs 只在 BUSINESS_EVENT 允许时保留；
    valuation_needs 只在 valuation 允许时保留（valuation 不由用户模块
    选择，而由 `include_relative_valuation` 显式开启）；
  - document_needs 保留（文档是各模块共同的研究资料输入），但
    macro_dataset 文档只在 MACRO 允许时保留；
  - 过滤后 analysis_modules / research_scope 为空 → `ResearchPlanScopeMismatch`
    （planner 输出完全落在用户选择之外——确定性失败，由上层重新生成）。

该函数在 `create_plan` 中于 LLM 生成之后、fingerprint / 持久化之前应用：
持久化的计划即过滤后的计划（plan fingerprint 覆盖过滤结果）。
"""

from app.domain.tasks import ResearchModule
from app.research_planning.contracts import (
    AnalysisModule,
    ResearchDocumentNeedType,
    ResearchPlanPayload,
    ResearchScope,
)
from app.research_planning.errors import ResearchPlanScopeMismatch

# 用户 ResearchModule → planner AnalysisModule 映射。
_MODULE_TO_PLANNER: dict[str, frozenset[AnalysisModule]] = {
    ResearchModule.COMPANY_PROFILE.value: frozenset({AnalysisModule.BUSINESS_EVENT}),
    ResearchModule.BUSINESS.value: frozenset({AnalysisModule.BUSINESS_EVENT}),
    ResearchModule.EVENTS.value: frozenset({AnalysisModule.BUSINESS_EVENT}),
    ResearchModule.FINANCIAL.value: frozenset({AnalysisModule.FINANCIAL}),
    ResearchModule.MACRO.value: frozenset({AnalysisModule.MACRO}),
    ResearchModule.RISK.value: frozenset({AnalysisModule.RISK}),
}

# 用户 ResearchModule → 允许的 ResearchScope 值。
_MODULE_TO_SCOPES: dict[str, frozenset[ResearchScope]] = {
    ResearchModule.COMPANY_PROFILE.value: frozenset({ResearchScope.BUSINESS}),
    ResearchModule.BUSINESS.value: frozenset({ResearchScope.BUSINESS}),
    ResearchModule.EVENTS.value: frozenset({ResearchScope.EVENT}),
    ResearchModule.FINANCIAL.value: frozenset({ResearchScope.FINANCIAL}),
    ResearchModule.MACRO.value: frozenset({ResearchScope.MACRO}),
    ResearchModule.RISK.value: frozenset({ResearchScope.RISK}),
}


def allowed_planner_modules(selected: list[str] | list[ResearchModule]) -> frozenset[str]:
    """用户选择的模块 → 允许的 planner AnalysisModule 值集合（去重并集）。"""
    allowed: set[AnalysisModule] = set()
    for module in selected:
        value = module.value if isinstance(module, ResearchModule) else str(module)
        allowed.update(_MODULE_TO_PLANNER.get(value, ()))
    return frozenset(m.value for m in allowed)


def allowed_scopes(selected: list[str] | list[ResearchModule]) -> frozenset[str]:
    """用户选择的模块 → 允许的 ResearchScope 值集合（去重并集）。"""
    allowed: set[ResearchScope] = set()
    for module in selected:
        value = module.value if isinstance(module, ResearchModule) else str(module)
        allowed.update(_MODULE_TO_SCOPES.get(value, ()))
    return frozenset(s.value for s in allowed)


def apply_selected_modules(
    payload: ResearchPlanPayload,
    selected: list[str] | list[ResearchModule],
    *,
    include_relative_valuation: bool = False,
) -> ResearchPlanPayload:
    """过滤 plan payload 到用户选择的模块范围（纯函数，确定性）。

    valuation 是额外开关（task.include_relative_valuation），不来自用户模块
    列表；未开启时删除 valuation 模块与 valuation_needs。

    `selected` 为空（历史任务 / eval runner 未声明模块）→ **不约束**：原样
    返回 payload（planner 自由生成，保持向后兼容）。
    """
    if not selected:
        return payload
    planner_values = allowed_planner_modules(selected)
    scope_values = allowed_scopes(selected)
    if include_relative_valuation:
        planner_values = planner_values | {AnalysisModule.VALUATION.value}
        scope_values = scope_values | {ResearchScope.VALUATION.value}

    keep_modules = [m for m in payload.analysis_modules if m.value in planner_values]
    keep_scopes = [s for s in payload.research_scope if s.value in scope_values]
    if not keep_modules:
        raise ResearchPlanScopeMismatch("planner 输出不含用户选择模块的任何分析模块")
    if not keep_scopes:
        raise ResearchPlanScopeMismatch("planner 输出不含用户选择模块的任何研究范围")

    keep_macro = AnalysisModule.MACRO.value in planner_values
    keep_financial = AnalysisModule.FINANCIAL.value in planner_values
    keep_valuation = AnalysisModule.VALUATION.value in planner_values
    # 事件模块是用户显式选择（ResearchModule.EVENTS）；未选择时剔除 event_needs
    # 与新闻/公告/IR 文档需求——这些是事件驱动的资料输入（且新闻在生产路径
    # 需要原创发布者验证链、公告/IR 为目的事件型资料，无法可靠自动匹配），
    # 保留会卡死自动研究。定期报告（年报/半年报/季报）是通用输入，恒保留。
    keep_events = ResearchModule.EVENTS.value in {
        m.value if isinstance(m, ResearchModule) else str(m) for m in selected
    }
    _EVENT_DRIVEN_DOC_TYPES = frozenset(
        {
            ResearchDocumentNeedType.NEWS_ARTICLE,
            ResearchDocumentNeedType.COMPANY_ANNOUNCEMENT,
            ResearchDocumentNeedType.ISSUER_IR_MATERIAL,
        }
    )

    return ResearchPlanPayload(
        research_scope=keep_scopes,
        document_needs=[
            need
            for need in payload.document_needs
            if (keep_macro or need.source_type != ResearchDocumentNeedType.MACRO_DATASET)
            and (keep_events or need.source_type not in _EVENT_DRIVEN_DOC_TYPES)
        ],
        financial_needs=(payload.financial_needs if keep_financial else []),
        macro_needs=(payload.macro_needs if keep_macro else []),
        event_needs=(payload.event_needs if keep_events else []),
        valuation_needs=(payload.valuation_needs if keep_valuation else []),
        analysis_modules=keep_modules,
        research_focus=payload.research_focus,
    )
