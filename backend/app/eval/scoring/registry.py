"""Deterministic metric registry (stage 7B.1.2A).

只注册「真正公平」的确定性指标 calculator（citation_validity /
citation_coverage）。其它 deterministic-kind 指标（claim_support_rate /
unsupported_claim_ratio / conflict_preservation / completion_rate /
node_failure_rate / recovery_success_rate / duplicate_write_rate /
human_resume_success_rate / research_backflow_success_rate）在 7B.1.2A 尚无
calculator，调用方通过 `calculate_available_deterministic_metrics()` 获知当前
可计算集合；对未实现指标请求 calculator 抛 `EvalScoringError`。
"""

from app.eval.errors import EvalScoringError
from app.eval.metrics import MetricName
from app.eval.scoring.deterministic import (
    CitationCoverageCalculator,
    CitationValidityCalculator,
    DeterministicMetricCalculator,
)

_DETERMINISTIC_CALCULATORS: dict[MetricName, DeterministicMetricCalculator] = {
    MetricName.CITATION_VALIDITY: CitationValidityCalculator(),
    MetricName.CITATION_COVERAGE: CitationCoverageCalculator(),
}


def calculate_available_deterministic_metrics() -> tuple[MetricName, ...]:
    """当前可计算的 deterministic 指标名（顺序稳定，与注册表插入序一致）。"""
    return tuple(_DETERMINISTIC_CALCULATORS)


def get_deterministic_calculator(name: MetricName) -> DeterministicMetricCalculator:
    """按名取 calculator；未实现 / 非 deterministic 指标抛 `EvalScoringError`。"""
    if name not in _DETERMINISTIC_CALCULATORS:
        raise EvalScoringError(f"deterministic metric not available: {name.value}")
    return _DETERMINISTIC_CALCULATORS[name]
