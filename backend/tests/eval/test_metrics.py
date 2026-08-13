"""Metrics surface 与 MetricValue 不变量测试（stage 7B.1.0）。"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.eval.metrics import (
    METRIC_SPECS,
    MetricDimension,
    MetricKind,
    MetricName,
    MetricStatus,
    MetricValue,
)


def test_metric_specs_cover_all_names() -> None:
    assert set(METRIC_SPECS) == set(MetricName)


def test_metric_surface_exact() -> None:
    expected = {
        "financial_accuracy",
        "citation_validity",
        "citation_coverage",
        "claim_support_rate",
        "unsupported_claim_ratio",
        "risk_topic_recall",
        "macro_causal_error_rate",
        "conflict_preservation",
        "overclaim_rate",
        "completion_rate",
        "node_failure_rate",
        "retry_count",
        "recovery_success_rate",
        "duplicate_write_rate",
        "human_resume_success_rate",
        "research_backflow_success_rate",
        "latency_ms",
        "llm_call_count",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "estimated_cost",
    }
    assert {m.value for m in MetricName} == expected
    assert len(MetricName) == 22


def test_spec_mappings() -> None:
    assert METRIC_SPECS[MetricName.CITATION_VALIDITY].dimension == MetricDimension.CONTENT_QUALITY
    assert METRIC_SPECS[MetricName.CITATION_VALIDITY].kind == MetricKind.DETERMINISTIC
    assert METRIC_SPECS[MetricName.CITATION_VALIDITY].higher_is_better is True
    assert METRIC_SPECS[MetricName.UNSUPPORTED_CLAIM_RATIO].higher_is_better is False
    assert METRIC_SPECS[MetricName.LATENCY_MS].dimension == MetricDimension.EFFICIENCY
    assert METRIC_SPECS[MetricName.COMPLETION_RATE].dimension == MetricDimension.RELIABILITY
    assert METRIC_SPECS[MetricName.ESTIMATED_COST].kind == MetricKind.RUNTIME
    assert METRIC_SPECS[MetricName.FINANCIAL_ACCURACY].kind == MetricKind.HUMAN_LABELED


def test_computed_requires_value() -> None:
    MetricValue(metric_name=MetricName.LATENCY_MS, status=MetricStatus.COMPUTED, value=Decimal("1"))
    with pytest.raises(ValidationError):
        MetricValue(metric_name=MetricName.LATENCY_MS, status=MetricStatus.COMPUTED)


def test_non_computed_forbids_value() -> None:
    MetricValue(metric_name=MetricName.CITATION_COVERAGE, status=MetricStatus.NOT_APPLICABLE)
    with pytest.raises(ValidationError):
        MetricValue(
            metric_name=MetricName.CITATION_COVERAGE,
            status=MetricStatus.NOT_APPLICABLE,
            value=Decimal("0"),
        )


def test_zero_denominator_rejected() -> None:
    # 「无 citation-eligible claim」用 not_applicable，不用 value=0 / denominator=0
    with pytest.raises(ValidationError):
        MetricValue(
            metric_name=MetricName.CITATION_COVERAGE,
            status=MetricStatus.COMPUTED,
            value=Decimal("0"),
            numerator=Decimal("0"),
            denominator=Decimal("0"),
        )


def test_negative_sample_count_rejected() -> None:
    with pytest.raises(ValidationError):
        MetricValue(
            metric_name=MetricName.LLM_CALL_COUNT,
            status=MetricStatus.COMPUTED,
            value=Decimal("1"),
            sample_count=-1,
        )
