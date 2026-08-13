"""Deterministic cross-variant metrics (stage 7B.1.2A)."""

from app.eval.scoring.context import EvalScoringContext
from app.eval.scoring.deterministic import (
    CitationCoverageCalculator,
    CitationValidityCalculator,
    DeterministicMetricCalculator,
    valid_source_fingerprints,
    verify_variant_output_structure,
)
from app.eval.scoring.registry import (
    calculate_available_deterministic_metrics,
    get_deterministic_calculator,
)

__all__ = [
    "EvalScoringContext",
    "DeterministicMetricCalculator",
    "CitationValidityCalculator",
    "CitationCoverageCalculator",
    "valid_source_fingerprints",
    "verify_variant_output_structure",
    "calculate_available_deterministic_metrics",
    "get_deterministic_calculator",
]
