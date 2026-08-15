"""Unit tests for planner module-scope enforcement (V1.1 closure)."""

import pytest

from app.research_planning.contracts import (
    AnalysisModule,
    DocumentNeed,
    FinancialNeed,
    MacroNeed,
    ResearchDocumentNeedType,
    ResearchPlanPayload,
    ResearchScope,
)
from app.research_planning.errors import ResearchPlanScopeMismatch
from app.research_planning.plan_scope import (
    allowed_planner_modules,
    allowed_scopes,
    apply_selected_modules,
)
from app.financial.calculations.contracts import CalculationCode
from app.financial.contracts import MetricCode


def _payload(**overrides) -> ResearchPlanPayload:
    base = dict(
        research_scope=[
            ResearchScope.BUSINESS,
            ResearchScope.EVENT,
            ResearchScope.FINANCIAL,
            ResearchScope.MACRO,
        ],
        document_needs=[
            DocumentNeed(
                need_code="annual_report_2023",
                purpose="2023 年度报告",
                source_type=ResearchDocumentNeedType.ANNUAL_REPORT,
                period="2023",
            ),
            DocumentNeed(
                need_code="macro_dataset_gdp",
                purpose="GDP 数据集",
                source_type=ResearchDocumentNeedType.MACRO_DATASET,
            ),
        ],
        financial_needs=[
            FinancialNeed(
                need_code="revenue_growth",
                purpose="营收增长",
                calculation_code=CalculationCode.YOY_GROWTH_RATE,
                metric_code=MetricCode.REVENUE,
                period="2023",
            )
        ],
        macro_needs=[
            MacroNeed(need_code="gdp_growth", purpose="GDP 增长", topic_or_indicator="GDP增长率")
        ],
        analysis_modules=[
            AnalysisModule.BUSINESS_EVENT,
            AnalysisModule.FINANCIAL,
            AnalysisModule.MACRO,
        ],
        research_focus=["营收结构"],
    )
    base.update(overrides)
    return ResearchPlanPayload(**base)


def test_allowed_planner_modules_mapping() -> None:
    assert allowed_planner_modules(["business", "events", "company_profile"]) == {
        "business_event"
    }
    assert allowed_planner_modules(["financial"]) == {"financial"}
    assert allowed_planner_modules(["macro"]) == {"macro"}
    assert allowed_planner_modules(["risk"]) == {"risk"}
    assert allowed_planner_modules(["business", "financial"]) == {
        "business_event",
        "financial",
    }


def test_allowed_scopes_mapping() -> None:
    assert allowed_scopes(["business", "events"]) == {"business", "event"}
    assert allowed_scopes(["financial"]) == {"financial"}


def test_apply_selected_modules_keeps_only_selected() -> None:
    payload = _payload()
    filtered = apply_selected_modules(payload, ["financial"])
    assert [m.value for m in filtered.analysis_modules] == ["financial"]
    assert [s.value for s in filtered.research_scope] == ["financial"]
    assert [n.need_code for n in filtered.financial_needs] == ["revenue_growth"]
    assert filtered.macro_needs == []
    assert filtered.event_needs == []
    # document_needs 保留，但 macro_dataset 文档被剔除。
    assert [n.need_code for n in filtered.document_needs] == ["annual_report_2023"]


def test_apply_selected_modules_business_event() -> None:
    payload = _payload()
    filtered = apply_selected_modules(payload, ["business", "events"])
    assert [m.value for m in filtered.analysis_modules] == ["business_event"]
    assert [s.value for s in filtered.research_scope] == ["business", "event"]
    assert filtered.macro_needs == []
    assert filtered.financial_needs == []
    # 未选 macro → macro_dataset 文档剔除；年度报告保留。
    assert [n.need_code for n in filtered.document_needs] == ["annual_report_2023"]


def test_apply_selected_modules_macro_keeps_macro_dataset() -> None:
    payload = _payload()
    filtered = apply_selected_modules(payload, ["macro"])
    assert [m.value for m in filtered.analysis_modules] == ["macro"]
    assert filtered.macro_needs
    # 选 macro → macro_dataset 文档保留（年度报告也保留：文档是通用资料输入）。
    assert [n.need_code for n in filtered.document_needs] == [
        "annual_report_2023",
        "macro_dataset_gdp",
    ]


def test_apply_selected_modules_valuation_gated_by_flag() -> None:
    payload = _payload(
        analysis_modules=[
            AnalysisModule.BUSINESS_EVENT,
            AnalysisModule.VALUATION,
        ],
        research_scope=[ResearchScope.BUSINESS, ResearchScope.VALUATION],
    )
    # 未开启 include_relative_valuation → valuation 剔除。
    filtered = apply_selected_modules(payload, ["business"])
    assert [m.value for m in filtered.analysis_modules] == ["business_event"]
    assert [s.value for s in filtered.research_scope] == ["business"]
    # 开启 → 保留。
    filtered = apply_selected_modules(payload, ["business"], include_relative_valuation=True)
    assert [m.value for m in filtered.analysis_modules] == ["business_event", "valuation"]


def test_apply_selected_modules_empty_intersection_raises() -> None:
    payload = _payload(analysis_modules=[AnalysisModule.FINANCIAL])
    with pytest.raises(ResearchPlanScopeMismatch):
        apply_selected_modules(payload, ["business"])


def test_apply_selected_modules_empty_scope_raises() -> None:
    payload = _payload(research_scope=[ResearchScope.FINANCIAL])
    with pytest.raises(ResearchPlanScopeMismatch):
        apply_selected_modules(payload, ["business"])


def test_apply_selected_modules_events_gate() -> None:
    from app.research_planning.contracts import EventNeed

    payload = _payload(
        event_needs=[EventNeed(need_code="evt_cap", purpose="产能扩张事件", topic="产能扩张")],
        document_needs=[
            DocumentNeed(
                need_code="news_industry",
                purpose="行业新闻",
                source_type=ResearchDocumentNeedType.NEWS_ARTICLE,
            ),
            DocumentNeed(
                need_code="ann_capacity",
                purpose="产能公告",
                source_type=ResearchDocumentNeedType.COMPANY_ANNOUNCEMENT,
            ),
            DocumentNeed(
                need_code="ir_material",
                purpose="IR 材料",
                source_type=ResearchDocumentNeedType.ISSUER_IR_MATERIAL,
            ),
            DocumentNeed(
                need_code="annual_2023",
                purpose="2023 年报",
                source_type=ResearchDocumentNeedType.ANNUAL_REPORT,
                period="2023",
            ),
        ],
    )
    # 未选 events → event_needs 与新闻/公告/IR 文档需求剔除；定期报告保留。
    filtered = apply_selected_modules(payload, ["business"])
    assert filtered.event_needs == []
    assert [n.need_code for n in filtered.document_needs] == ["annual_2023"]
    # 选 events → 全部保留。
    filtered = apply_selected_modules(payload, ["business", "events"])
    assert [n.need_code for n in filtered.event_needs] == ["evt_cap"]
    assert [n.need_code for n in filtered.document_needs] == [
        "news_industry",
        "ann_capacity",
        "ir_material",
        "annual_2023",
    ]
