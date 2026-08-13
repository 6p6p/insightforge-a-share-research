"""LLM usage → runtime metric 聚合 (stage 7B.1.2B).

把一次 execution 收集的 `LlmCallUsageRecord` 集合聚合为 `llm_call_count` +
input/output/total tokens 四个 `MetricValue`：

- `llm_call_count`：计**所有**尝试（含 parsing_error / invocation_error）；
- token 指标：仅当所有 record `usage_status=reported` 且 token 完整才 computed
  （求和）；任一 unavailable → `unavailable`（reason_code=incomplete_llm_usage）；
- 0 call → `llm_call_count=0` computed，token 指标 computed=0。

不计算 `estimated_cost`（7B.1.2B 范围外）；不把 per-call duration 求和映射到
`latency_ms`（7B.1.2B 只捕获 duration，不做 latency 语义映射）。
"""

from __future__ import annotations

from decimal import Decimal

from app.eval.metrics import MetricName, MetricStatus, MetricValue
from app.llm.instrumentation import LlmCallUsageRecord, UsageStatus

_METRIC_VERSION = 1

_TOKEN_FIELD: dict[MetricName, str] = {
    MetricName.INPUT_TOKENS: "input_tokens",
    MetricName.OUTPUT_TOKENS: "output_tokens",
    MetricName.TOTAL_TOKENS: "total_tokens",
}


def aggregate_llm_usage(
    records: tuple[LlmCallUsageRecord, ...],
) -> dict[MetricName, MetricValue]:
    """聚合 usage records → runtime 指标值。返回 keys 恒为 4 个 efficiency 指标。"""
    call_count = len(records)
    values: dict[MetricName, MetricValue] = {
        MetricName.LLM_CALL_COUNT: MetricValue(
            metric_name=MetricName.LLM_CALL_COUNT,
            metric_version=_METRIC_VERSION,
            status=MetricStatus.COMPUTED,
            value=Decimal(call_count),
            numerator=Decimal(call_count),
            sample_count=call_count,
        )
    }
    incomplete = any(r.usage_status != UsageStatus.REPORTED for r in records)
    for name, field in _TOKEN_FIELD.items():
        if incomplete:
            values[name] = MetricValue(
                metric_name=name,
                metric_version=_METRIC_VERSION,
                status=MetricStatus.UNAVAILABLE,
                sample_count=call_count,
                reason_code="incomplete_llm_usage",
            )
        else:
            total = sum(getattr(r, field) for r in records)
            values[name] = MetricValue(
                metric_name=name,
                metric_version=_METRIC_VERSION,
                status=MetricStatus.COMPUTED,
                value=Decimal(total),
                numerator=Decimal(total),
                sample_count=call_count,
            )
    return values
