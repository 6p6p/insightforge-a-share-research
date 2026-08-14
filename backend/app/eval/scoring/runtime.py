"""Runtime metric calculators (stage 7B.1.3D).

从 attempt 结果 + usage records 计算 **runtime 指标**（latency / tokens /
call count / estimated cost）与 **deterministic reliability 指标**
（completion_rate）。全部纯函数（0 LLM / 0 DB / 0 network）；cost 只走
`PricingSnapshot`（versioned，显式记录 pricing_version，不硬编码进语义逻辑）。
"""

from decimal import Decimal

from app.eval.metrics import MetricName, MetricStatus, MetricValue
from app.eval.pricing import PricingSnapshot
from app.eval.scoring.context import EvalScoringContext
from app.eval.scoring.deterministic import DeterministicMetricCalculator
from app.llm.instrumentation import UsageStatus

_METRIC_VERSION = 1


class LatencyMsCalculator(DeterministicMetricCalculator):
    name = MetricName.LATENCY_MS

    def calculate(self, context: EvalScoringContext) -> MetricValue:
        latency = getattr(context, "wall_latency_ms", None)
        if latency is None:
            return MetricValue(
                metric_name=self.name,
                metric_version=_METRIC_VERSION,
                status=MetricStatus.UNAVAILABLE,
                reason_code="no_attempt",
            )
        return MetricValue(
            metric_name=self.name,
            metric_version=_METRIC_VERSION,
            status=MetricStatus.COMPUTED,
            value=Decimal(latency),
        )


class _TokenUsageCalculator(DeterministicMetricCalculator):
    """input / output / total token 的共享实现（从 usage records 聚合）。"""

    _field = "total_tokens"

    def _sum(self, context: EvalScoringContext) -> int | None:
        records = getattr(context, "usage_records", None)
        if not records:
            return None
        total = 0
        any_reported = False
        for record in records:
            if record.usage_status != UsageStatus.REPORTED:
                continue
            value = getattr(record, self._field)
            if value is None:
                return None
            total += value
            any_reported = True
        return total if any_reported else None

    def calculate(self, context: EvalScoringContext) -> MetricValue:
        value = self._sum(context)
        if value is None:
            return MetricValue(
                metric_name=self.name,
                metric_version=_METRIC_VERSION,
                status=MetricStatus.UNAVAILABLE,
                reason_code="no_reported_usage",
            )
        return MetricValue(
            metric_name=self.name,
            metric_version=_METRIC_VERSION,
            status=MetricStatus.COMPUTED,
            value=Decimal(value),
        )


class InputTokensCalculator(_TokenUsageCalculator):
    name = MetricName.INPUT_TOKENS
    _field = "input_tokens"


class OutputTokensCalculator(_TokenUsageCalculator):
    name = MetricName.OUTPUT_TOKENS
    _field = "output_tokens"


class TotalTokensCalculator(_TokenUsageCalculator):
    name = MetricName.TOTAL_TOKENS
    _field = "total_tokens"


class LlmCallCountCalculator(DeterministicMetricCalculator):
    name = MetricName.LLM_CALL_COUNT

    def calculate(self, context: EvalScoringContext) -> MetricValue:
        records = getattr(context, "usage_records", None)
        if not records:
            return MetricValue(
                metric_name=self.name,
                metric_version=_METRIC_VERSION,
                status=MetricStatus.UNAVAILABLE,
                reason_code="no_usage",
            )
        return MetricValue(
            metric_name=self.name,
            metric_version=_METRIC_VERSION,
            status=MetricStatus.COMPUTED,
            value=Decimal(len(records)),
        )


class EstimatedCostCalculator(DeterministicMetricCalculator):
    name = MetricName.ESTIMATED_COST

    def __init__(self, pricing: PricingSnapshot | None = None) -> None:
        self._pricing = pricing or PricingSnapshot()
        # 显式记录 pricing version（成本依赖易变 vendor 价格）。
        self.pricing_version = self._pricing.version

    def calculate(self, context: EvalScoringContext) -> MetricValue:
        records = getattr(context, "usage_records", None)
        if not records:
            return MetricValue(
                metric_name=self.name,
                metric_version=_METRIC_VERSION,
                status=MetricStatus.UNAVAILABLE,
                reason_code="no_usage",
            )
        cost = self._pricing.estimate_cost(tuple(records))
        if cost is None:
            return MetricValue(
                metric_name=self.name,
                metric_version=_METRIC_VERSION,
                status=MetricStatus.UNAVAILABLE,
                reason_code="unknown_pricing",
            )
        return MetricValue(
            metric_name=self.name,
            metric_version=_METRIC_VERSION,
            status=MetricStatus.COMPUTED,
            value=Decimal(str(round(cost, 12))),
        )


class CompletionRateCalculator(DeterministicMetricCalculator):
    name = MetricName.COMPLETION_RATE

    def calculate(self, context: EvalScoringContext) -> MetricValue:
        status = getattr(context, "attempt_status", None)
        if status is None:
            return MetricValue(
                metric_name=self.name,
                metric_version=_METRIC_VERSION,
                status=MetricStatus.UNAVAILABLE,
                reason_code="no_attempt",
            )
        completed = 1 if status == "success" else 0
        return MetricValue(
            metric_name=self.name,
            metric_version=_METRIC_VERSION,
            status=MetricStatus.COMPUTED,
            value=Decimal(completed),
            numerator=Decimal(completed),
            denominator=Decimal(1),
            sample_count=1,
        )


RUNTIME_CALCULATORS: dict[MetricName, DeterministicMetricCalculator] = {
    MetricName.LATENCY_MS: LatencyMsCalculator(),
    MetricName.LLM_CALL_COUNT: LlmCallCountCalculator(),
    MetricName.INPUT_TOKENS: InputTokensCalculator(),
    MetricName.OUTPUT_TOKENS: OutputTokensCalculator(),
    MetricName.TOTAL_TOKENS: TotalTokensCalculator(),
    MetricName.ESTIMATED_COST: EstimatedCostCalculator(),
    MetricName.COMPLETION_RATE: CompletionRateCalculator(),
}
