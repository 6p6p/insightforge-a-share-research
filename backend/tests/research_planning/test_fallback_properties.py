"""Property tests for deterministic fallback plan builder (P1 + P9).

Verifies that fallback plans are always schema-valid regardless of input.
No company-specific fixtures; pure property-based tests.
"""

from datetime import date

import pytest

from app.domain.tasks import ResearchModule
from app.research_planning.fallback import build_fallback_plan_payload
from app.research_planning.contracts import ResearchPlanPayload


class TestFallbackPlanProperties:
    """Property/invariant tests: fallback plan is always valid."""

    @pytest.mark.parametrize(
        "modules,question",
        [
            # Baseline: all modules, generic question
            (
                [
                    ResearchModule.COMPANY_PROFILE.value,
                    ResearchModule.BUSINESS.value,
                    ResearchModule.FINANCIAL.value,
                    ResearchModule.RISK.value,
                ],
                "分析公司经营质量和风险",
            ),
            # Company-only: no explicit question
            (
                [
                    ResearchModule.COMPANY_PROFILE.value,
                    ResearchModule.BUSINESS.value,
                    ResearchModule.FINANCIAL.value,
                    ResearchModule.RISK.value,
                ],
                "公司基本面分析",
            ),
            # Minimal: only company_profile
            ([ResearchModule.COMPANY_PROFILE.value], "公司概况"),
            # Empty modules (safety net)
            ([], "分析公司"),
            # All 6 modules
            (
                list(ResearchModule),
                "全面分析",
            ),
        ],
    )
    def test_fallback_plan_always_schema_valid(self, modules, question):
        """Property: fallback plan must pass model_validator for any valid input."""
        plan = build_fallback_plan_payload(
            modules=modules,
            research_question=question,
            analysis_as_of=date(2026, 8, 17),
        )
        # If model_validator passes, we have a valid ResearchPlanPayload
        assert isinstance(plan, ResearchPlanPayload)
        assert len(plan.research_scope) >= 1
        assert len(plan.analysis_modules) >= 1

    def test_fallback_plan_deterministic(self):
        """Same input → same output (fingerprint-stable)."""
        modules = [
            ResearchModule.COMPANY_PROFILE.value,
            ResearchModule.FINANCIAL.value,
        ]
        q = "分析公司财务状况"
        d = date(2026, 8, 17)

        p1 = build_fallback_plan_payload(
            modules=modules, research_question=q, analysis_as_of=d
        )
        p2 = build_fallback_plan_payload(
            modules=modules, research_question=q, analysis_as_of=d
        )
        assert p1.normalized_payload() == p2.normalized_payload()

    def test_fallback_plan_no_company_specific_branches(self):
        """Property: fallback must NOT contain company-specific logic."""
        # The fallback builder is a pure function with no company_id parameter.
        # The only varying input is modules, question, analysis_as_of.
        # Verify the source code has no 'if company' / 'if security_code' patterns.
        import inspect
        source = inspect.getsource(build_fallback_plan_payload)
        forbidden = ["security_code", "company_name", "company_id", "600519", "300750"]
        for token in forbidden:
            assert token not in source, f"Company-specific token '{token}' found in fallback builder"

    def test_fallback_plan_respects_modules(self):
        """Fallback with only FINANCIAL module → must have financial scope/needs."""
        plan = build_fallback_plan_payload(
            modules=[ResearchModule.FINANCIAL.value],
            research_question="分析财务状况",
            analysis_as_of=date(2026, 8, 17),
        )
        # After apply_selected_modules, only FINANCIAL should remain
        from app.research_planning.contracts import ResearchScope
        assert ResearchScope.FINANCIAL in plan.research_scope
        # Financial needs must be present
        assert len(plan.financial_needs) > 0
