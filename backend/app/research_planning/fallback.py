"""Deterministic fallback plan builder (P1: Planner must not be a single point of failure).

When LLM Planner malformed output exhausts bounded retries, this module builds a
valid ResearchPlanPayload that:

- Is always schema-valid (passes model_validator)
- Is company-generic: NO company-specific logic, NO security-code branches
- Covers the minimum viable baseline: company_profile / business / financial / risk
- Respects selected modules via apply_selected_modules
- Uses minimal generic context_needs (missing advanced context does not block)

The fallback is a safety net — research continues with reduced depth rather than
failing entirely.
"""

from datetime import date

from app.domain.tasks import ResearchModule
from app.financial.calculations.contracts import CalculationCode
from app.financial.contracts import MetricCode
from app.research_planning.contracts import (
    AnalysisModule,
    ContextNeed,
    DocumentNeed,
    FinancialNeed,
    ResearchDocumentNeedType,
    ResearchPlanPayload,
    ResearchScope,
)
from app.research_planning.plan_scope import apply_selected_modules

# Maximum retries before fallback.
MAX_PLANNER_RETRIES = 3


def build_fallback_plan_payload(
    *,
    modules: list[str],
    research_question: str,
    analysis_as_of: date,
) -> ResearchPlanPayload:
    """Build a deterministic, schema-valid fallback ResearchPlanPayload.

    Covers the universal baseline: company_profile / business / financial / risk.
    event and macro are included only if user explicitly selected them.
    valuation is omitted (no relative valuation in fallback).
    context_needs are empty (missing advanced context does not block per P2).

    This function is PURE — no DB, no LLM, no I/O.
    """
    module_set = set(modules)
    need_business_event = any(
        m in module_set
        for m in (
            ResearchModule.COMPANY_PROFILE.value,
            ResearchModule.BUSINESS.value,
            ResearchModule.EVENTS.value,
        )
    )
    need_financial = ResearchModule.FINANCIAL.value in module_set
    need_macro = ResearchModule.MACRO.value in module_set
    need_risk = ResearchModule.RISK.value in module_set

    # research_scope: from selected modules
    scopes: list[ResearchScope] = []
    if need_business_event:
        scopes.append(ResearchScope.BUSINESS)
    if need_financial:
        scopes.append(ResearchScope.FINANCIAL)
    if need_macro:
        scopes.append(ResearchScope.MACRO)
    if need_risk:
        scopes.append(ResearchScope.RISK)
    if not scopes:
        # safety: at minimum provide business scope
        scopes = [ResearchScope.BUSINESS]

    # analysis_modules: from selected modules
    mods: list[AnalysisModule] = []
    if need_business_event:
        mods.append(AnalysisModule.BUSINESS_EVENT)
    if need_financial:
        mods.append(AnalysisModule.FINANCIAL)
    if need_macro:
        mods.append(AnalysisModule.MACRO)
    if need_risk:
        mods.append(AnalysisModule.RISK)
    if not mods:
        mods = [AnalysisModule.BUSINESS_EVENT]

    # Trailing years for document needs (current year back 3 years)
    current_year = analysis_as_of.year
    years = [str(y) for y in range(current_year, current_year - 3, -1) if y >= 1990]

    # document_needs: annuals (3 trailing) + semiannual + quarterly
    doc_needs: list[DocumentNeed] = []
    for yr in years:
        doc_needs.append(
            DocumentNeed(
                need_code=f"annual_report_{yr}",
                purpose=f"{yr} 年年度报告",
                source_type=ResearchDocumentNeedType.ANNUAL_REPORT,
                period=yr,
            )
        )
    doc_needs.append(
        DocumentNeed(
            need_code=f"semiannual_report_{current_year}",
            purpose=f"{current_year} 年半年报告",
            source_type=ResearchDocumentNeedType.SEMIANNUAL_REPORT,
            period=str(current_year),
        )
    )
    doc_needs.append(
        DocumentNeed(
            need_code=f"quarterly_report_{current_year}",
            purpose=f"{current_year} 年季度报告",
            source_type=ResearchDocumentNeedType.QUARTERLY_REPORT,
            period=str(current_year),
        )
    )

    # financial_needs: universal basic metrics
    fin_needs: list[FinancialNeed] = []
    if need_financial:
        fin_needs.append(
            FinancialNeed(
                need_code="revenue_yoy_growth",
                purpose="营业收入同比增长率",
                calculation_code=CalculationCode.YOY_GROWTH_RATE,
                metric_code=MetricCode.REVENUE,
            )
        )
        fin_needs.append(
            FinancialNeed(
                need_code="net_profit_parent_yoy_growth",
                purpose="归母净利润同比增长率",
                calculation_code=CalculationCode.YOY_GROWTH_RATE,
                metric_code=MetricCode.NET_PROFIT_PARENT,
            )
        )
        fin_needs.append(
            FinancialNeed(
                need_code="gross_margin",
                purpose="毛利率",
                calculation_code=CalculationCode.GROSS_MARGIN,
            )
        )
        fin_needs.append(
            FinancialNeed(
                need_code="operating_margin",
                purpose="营业利润率",
                calculation_code=CalculationCode.OPERATING_MARGIN,
            )
        )
        fin_needs.append(
            FinancialNeed(
                need_code="net_margin_parent",
                purpose="归母净利率",
                calculation_code=CalculationCode.NET_MARGIN_PARENT,
            )
        )
        fin_needs.append(
            FinancialNeed(
                need_code="debt_to_assets_ratio",
                purpose="资产负债率",
                calculation_code=CalculationCode.DEBT_TO_ASSETS_RATIO,
            )
        )

    # context_needs: empty — missing context does not block study (P2)
    ctx_needs: list[ContextNeed] = []

    # research_focus: derived from question, max 40 chars
    q = research_question.strip() if research_question else ""
    focus = q[:40] if q else "公司基本面分析"

    payload = ResearchPlanPayload(
        research_scope=scopes,
        analysis_modules=mods,
        document_needs=doc_needs,
        financial_needs=fin_needs,
        macro_needs=[],
        event_needs=[],
        valuation_needs=[],
        context_needs=ctx_needs,
        research_focus=[focus],
    )

    if modules:
        payload = apply_selected_modules(payload, modules, include_relative_valuation=False)

    return payload
