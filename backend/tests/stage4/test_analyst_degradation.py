"""Regression tests for Stage4 analyst degradation (P3).

Verifies: single analyst failure does not equal entire Stage4 failure.
Retryable errors trigger bounded retry then graceful degradation.
Hard failures still propagate.
"""

from uuid import uuid4

import pytest

from app.analysis.claims.errors import (
    ClaimAnalysisMalformedOutput,
    ClaimAnalysisModelUnavailable,
)
from app.analysis.financial.errors import (
    FinancialAnalysisMalformedOutput,
    FinancialAnalysisModelUnavailable,
    FinancialAnalysisNumericLiteralForbidden,
)
from app.stage4.analyst_error_policy import (
    classify_analyst_error,
    is_retryable,
)
from tests.stage4.test_stage4_graph import (
    StubClaimService,
    _invoke,
    _item,
    _request,
    _state,
    _uid,
    build_deps,
)

# ------------------------------------------------------------------ error classification


class TestAnalystErrorClassification:
    """Property tests for analyst error classification."""

    @pytest.mark.parametrize(
        "exc_cls,expected",
        [
            (ClaimAnalysisMalformedOutput, "retryable"),
            (ClaimAnalysisModelUnavailable, "retryable"),
            (FinancialAnalysisMalformedOutput, "retryable"),
            (FinancialAnalysisModelUnavailable, "retryable"),
            (FinancialAnalysisNumericLiteralForbidden, "retryable"),
        ],
    )
    def test_retryable_errors_are_retryable(self, exc_cls, expected):
        classification = classify_analyst_error(exc_cls())
        assert classification == expected

    def test_hard_failures_not_retryable(self):
        """Unknown / integrity errors are NOT retryable."""

        class SomeRandomException(Exception):
            pass

        assert not is_retryable(SomeRandomException())
        assert classify_analyst_error(SomeRandomException()) == "hard_failure"


# ------------------------------------------------------------------ degradation flow


@pytest.mark.asyncio
class TestAnalystDegradation:
    """End-to-end: individual analyst degradation -> Stage4 continues."""

    async def test_single_analyst_degraded_others_succeed(self):
        """P3: one analyst degraded, others produce claims -> continues."""
        business_card = uuid4()
        fin_calc = uuid4()

        call_count = [0]

        class DegradingClaimService(StubClaimService):
            async def analyze(self, request):
                call_count[0] += 1
                raise ClaimAnalysisMalformedOutput(f"simulated degrade (attempt {call_count[0]})")

        deps, _ = build_deps(
            claim_cls=DegradingClaimService,
            claim_ids={str(business_card): [_uid()]},
            financial_ids={str(fin_calc): [_uid(), _uid()]},
        )
        items = [
            _item("a", "business", evidence_card_ids=[business_card]),
            _item("b", "financial", calculation_ids=[fin_calc]),
        ]
        # Should NOT raise -- financial produces claims, business degraded
        final = await _invoke(deps, _state(_request(items)))
        assert len(final["analysis_results"]) == 2
        # Business should have empty claims (degraded)
        business_result = next(r for r in final["analysis_results"] if r["item_id"] == "a")
        assert business_result["claim_ids"] == []
        # Financial should have claims
        fin_result = next(r for r in final["analysis_results"] if r["item_id"] == "b")
        assert len(fin_result["claim_ids"]) > 0
        # Degraded items recorded
        assert len(final["degraded_items"]) == 1
        assert final["degraded_items"][0]["item_id"] == "a"

    async def test_all_analysts_degraded_raises(self):
        """All analysts degraded with 0 claims -> irrecoverable."""
        from app.stage4.errors import Stage4InsufficientClaims

        class AllDegradingClaimService(StubClaimService):
            async def analyze(self, request):
                raise ClaimAnalysisModelUnavailable("simulated degrade")

        deps, _ = build_deps(claim_cls=AllDegradingClaimService)
        items = [_item("a", "business", evidence_card_ids=[uuid4()])]
        with pytest.raises(Stage4InsufficientClaims):
            await _invoke(deps, _state(_request(items)))

    async def test_hard_failure_still_propagates(self):
        """Integrity/data errors must NOT be caught -- propagate immediately."""

        class HardFailingClaimService(StubClaimService):
            async def analyze(self, request):
                raise ValueError("data corruption -- must propagate")

        deps, _ = build_deps(claim_cls=HardFailingClaimService)
        items = [_item("a", "business", evidence_card_ids=[uuid4()])]
        with pytest.raises(ValueError):
            await _invoke(deps, _state(_request(items)))
