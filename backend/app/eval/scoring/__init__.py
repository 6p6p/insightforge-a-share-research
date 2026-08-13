"""Deterministic cross-variant metrics (stage 7B.1.2A)."""

from app.eval.scoring.context import EvalScoringContext
from app.eval.scoring.deterministic import (
    CitationAnalysis,
    CitationCoverageCalculator,
    CitationValidityCalculator,
    DeterministicMetricCalculator,
    analyze_citations,
    valid_source_fingerprints,
    verify_variant_output_identity,
)
from app.eval.scoring.registry import (
    calculate_available_deterministic_metrics,
    get_deterministic_calculator,
)

__all__ = [
    "EvalScoringContext",
    "DeterministicMetricCalculator",
    "CitationAnalysis",
    "CitationValidityCalculator",
    "CitationCoverageCalculator",
    "analyze_citations",
    "valid_source_fingerprints",
    "verify_variant_output_identity",
    "calculate_available_deterministic_metrics",
    "get_deterministic_calculator",
]
