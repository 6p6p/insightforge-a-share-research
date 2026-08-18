"""Analyst error classification for Stage4 graceful degradation (P3).

Determines whether an exception from an individual analyst should:
- RETRY: transient/model-format issue -> bounded retry
- DEGRADE: retries exhausted -> mark module degraded, others continue
- HARD_FAILURE: data integrity/corruption -> propagate to orchestration

Principle: single analyst failure != entire Stage4 failure.
Only irrecoverable integrity violations kill the orchestration.
"""

from app.analysis.claims.errors import (
    ClaimAnalysisMalformedOutput,
    ClaimAnalysisModelUnavailable,
)
from app.analysis.financial.errors import (
    FinancialAnalysisClaimKindPolicy,
    FinancialAnalysisMalformedOutput,
    FinancialAnalysisModelUnavailable,
    FinancialAnalysisNumericLiteralForbidden,
    FinancialAnalysisRelationConflict,
    FinancialAnalysisUnknownRef,
)
from app.analysis.macro.errors import (
    MacroAnalysisClaimKindPolicy,
    MacroAnalysisMalformedOutput,
    MacroAnalysisModelUnavailable,
    MacroAnalysisNumericLiteralForbidden,
    MacroAnalysisOverclaimPolicy,
    MacroAnalysisRelationConflict,
    MacroAnalysisUnknownRef,
)
from app.analysis.synthesis.errors import (
    SynthesisAnalysisMalformedOutput,
    SynthesisAnalysisModelUnavailable,
    SynthesisAnalysisNoCherryPicking,
    SynthesisAnalysisUnknownRef,
)
from app.analysis.valuation.errors import (
    ValuationAnalysisClaimDraftError,
    ValuationAnalysisDirectionConflict,
    ValuationAnalysisMalformedOutput,
    ValuationAnalysisMixedEvidenceInsufficient,
    ValuationAnalysisModelUnavailable,
    ValuationAnalysisRelationConflict,
    ValuationAnalysisUncertainImportancePolicy,
    ValuationAnalysisUnknownRef,
)

# Maximum retry attempts per analyst before degradation.
MAX_ANALYST_RETRIES = 3

# Error classes that should be retried (transient model/format issues).
_RETRYABLE_ERRORS = frozenset(
    {
        # Malformed structured output from all analysts
        ClaimAnalysisMalformedOutput,
        FinancialAnalysisMalformedOutput,
        MacroAnalysisMalformedOutput,
        ValuationAnalysisMalformedOutput,
        SynthesisAnalysisMalformedOutput,
        # Model unavailable from all analysts
        ClaimAnalysisModelUnavailable,
        FinancialAnalysisModelUnavailable,
        MacroAnalysisModelUnavailable,
        ValuationAnalysisModelUnavailable,
        SynthesisAnalysisModelUnavailable,
        # LLM policy violations (generated forbidden content -- retry before degrade)
        FinancialAnalysisNumericLiteralForbidden,
        FinancialAnalysisUnknownRef,
        FinancialAnalysisRelationConflict,
        FinancialAnalysisClaimKindPolicy,
        MacroAnalysisNumericLiteralForbidden,
        MacroAnalysisUnknownRef,
        MacroAnalysisRelationConflict,
        MacroAnalysisOverclaimPolicy,
        MacroAnalysisClaimKindPolicy,
        ValuationAnalysisUnknownRef,
        ValuationAnalysisRelationConflict,
        ValuationAnalysisDirectionConflict,
        ValuationAnalysisMixedEvidenceInsufficient,
        ValuationAnalysisUncertainImportancePolicy,
        ValuationAnalysisClaimDraftError,
        SynthesisAnalysisUnknownRef,
        SynthesisAnalysisNoCherryPicking,
    }
)


def is_retryable(exc: Exception) -> bool:
    """Return True if this exception class should trigger a bounded retry."""
    return type(exc) in _RETRYABLE_ERRORS


def classify_analyst_error(exc: Exception) -> str:
    """Classify an analyst exception.

    Returns:
        "retryable" -- transient model/format issue -> should retry
        "hard_failure" -- integrity/data corruption -> must propagate
    """
    if is_retryable(exc):
        return "retryable"
    return "hard_failure"
