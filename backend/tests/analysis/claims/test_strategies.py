"""Claim analysis strategies unit tests (stage 4B.1)。

校验 analysis_domain → strategy 映射与"未就绪 domain 拒绝"边界：
- business / event → business_event_v1；
- risk → risk_skeptic_v1；
- financial / macro / valuation → ClaimAnalysisDomainNotReady（不提前实现）；
- strategy_focus 提供非空分析重点（进入 user prompt，不进 system）。
"""

import pytest

from app.analysis.claims.errors import ClaimAnalysisDomainNotReady
from app.analysis.claims.strategies import (
    ANALYST_STRATEGY_BUSINESS_EVENT,
    ANALYST_STRATEGY_RISK,
    strategy_focus,
    strategy_for_domain,
)
from app.claims.contracts import ClaimAnalysisDomain


def test_business_and_event_map_to_business_event_v1() -> None:
    assert strategy_for_domain(ClaimAnalysisDomain.BUSINESS) == ANALYST_STRATEGY_BUSINESS_EVENT
    assert strategy_for_domain(ClaimAnalysisDomain.EVENT) == ANALYST_STRATEGY_BUSINESS_EVENT


def test_risk_maps_to_risk_skeptic_v1() -> None:
    assert strategy_for_domain(ClaimAnalysisDomain.RISK) == ANALYST_STRATEGY_RISK


def test_financial_macro_valuation_not_ready() -> None:
    for domain in (
        ClaimAnalysisDomain.FINANCIAL,
        ClaimAnalysisDomain.MACRO,
        ClaimAnalysisDomain.VALUATION,
    ):
        with pytest.raises(ClaimAnalysisDomainNotReady):
            strategy_for_domain(domain)


def test_strategy_focus_returns_nonempty_text() -> None:
    assert strategy_focus(ANALYST_STRATEGY_BUSINESS_EVENT)
    assert strategy_focus(ANALYST_STRATEGY_RISK)


def test_strategy_focus_unknown_strategy_empty() -> None:
    assert strategy_focus("no_such_strategy") == ""
