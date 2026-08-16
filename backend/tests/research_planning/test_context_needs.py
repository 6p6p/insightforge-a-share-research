"""Research Context Intelligence unit tests (P0/P1).

- ContextNeed / ContextNeedType 契约（bounded vocabulary / 校验边界）；
- ResearchPlanPayload 含 context_needs（数量上限 / need_code 全局唯一）；
- router route_context_need 确定性映射。
"""

import pytest
from pydantic import ValidationError

from app.research_planning.contracts import (
    ContextNeed,
    ContextNeedType,
    DocumentNeed,
    ResearchDocumentNeedType,
    ResearchPlanPayload,
)
from app.research_planning.router import (
    SourceRouteType,
    route_context_need,
    route_need,
)


def _context(**overrides) -> ContextNeed:
    base = dict(
        need_code="lithium_price",
        purpose="需要锂价走势",
        context_type=ContextNeedType.COMMODITY_MARKET,
        topic="锂价",
    )
    base.update(overrides)
    return ContextNeed(**base)


# ---------------------------------------------------------------- 契约


def test_context_need_valid() -> None:
    need = _context(geography="中国", period="2025")
    assert need.context_type == ContextNeedType.COMMODITY_MARKET
    assert need.topic == "锂价"


def test_context_need_rejects_blank_topic() -> None:
    with pytest.raises(ValidationError):
        _context(topic="   ")


def test_context_need_rejects_internal_id_topic() -> None:
    with pytest.raises(ValidationError):
        _context(topic="请研究 123e4567-e89b-12d3-a456-426614174000")


def test_context_need_rejects_bad_period() -> None:
    with pytest.raises(ValidationError):
        _context(period="2025H1")


def test_context_need_rejects_oversized_geography() -> None:
    with pytest.raises(ValidationError):
        _context(geography="国" * 30)


def test_payload_accepts_context_needs() -> None:
    payload = ResearchPlanPayload(
        research_scope=["business"],
        analysis_modules=["business_event"],
        context_needs=[
            _context(need_code="lithium_price"),
            _context(
                need_code="ev_sales",
                context_type=ContextNeedType.INDUSTRY_METRIC,
                topic="新能源汽车销量",
            ),
        ],
    )
    assert len(payload.context_needs) == 2


def test_payload_context_need_code_unique_across_all() -> None:
    with pytest.raises(ValidationError):
        ResearchPlanPayload(
            research_scope=["business"],
            analysis_modules=["business_event"],
            document_needs=[
                DocumentNeed(
                    need_code="dup_code",
                    purpose="需要年报",
                    source_type=ResearchDocumentNeedType.ANNUAL_REPORT,
                )
            ],
            context_needs=[_context(need_code="dup_code")],
        )


def test_payload_context_need_count_capped() -> None:
    with pytest.raises(ValidationError):
        ResearchPlanPayload(
            research_scope=["business"],
            analysis_modules=["business_event"],
            context_needs=[_context(need_code=f"ctx_{i}") for i in range(11)],
        )


# ---------------------------------------------------------------- router


def test_route_context_mappings() -> None:
    assert route_context_need("regulatory_policy") == (
        SourceRouteType.REGULATION,
        None,
    )
    assert route_context_need("industry_metric") == (
        SourceRouteType.NEWS_ARTICLE,
        "news_article",
    )
    assert route_context_need("commodity_market") == (
        SourceRouteType.NEWS_ARTICLE,
        "news_article",
    )
    assert route_context_need("macro_timeseries") == (
        SourceRouteType.MACRO_DATA,
        None,
    )
    assert route_context_need("company_ir") == (
        SourceRouteType.ISSUER_IR,
        "issuer_ir_material",
    )
    assert route_context_need("esg") == (
        SourceRouteType.ISSUER_IR,
        "issuer_ir_material",
    )
    assert route_context_need("investor_presentation") == (
        SourceRouteType.ISSUER_IR,
        "issuer_ir_material",
    )


def test_route_need_context_kind() -> None:
    assert route_need("context", "industry_metric") == (
        SourceRouteType.NEWS_ARTICLE,
        "news_article",
    )
    assert route_need("context", "macro_timeseries") == (
        SourceRouteType.MACRO_DATA,
        None,
    )
