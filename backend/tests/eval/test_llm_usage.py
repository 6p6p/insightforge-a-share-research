"""LLM usage 采集 + 聚合单测 (stage 7B.1.2B).

零真实 DeepSeek / 零网络：验证 `EvalLlmUsageCollector` 的 per-execution 绑定与
记录收集，以及 `aggregate_llm_usage` 的 `llm_call_count` + token 指标聚合语义。
"""

from decimal import Decimal

import pytest

from app.eval.metrics import MetricName, MetricStatus
from app.eval.usage import EvalLlmUsageCollector, aggregate_llm_usage
from app.eval.variants import EvalVariantId
from app.llm.instrumentation import LlmCallOutcome, LlmCallUsageRecord, UsageStatus

_EXEC_FP = "d" * 64


def _reported(
    *, input_tokens=10, output_tokens=5, total_tokens=15, outcome=None
) -> LlmCallUsageRecord:
    return LlmCallUsageRecord(
        component_name="evidence_extraction",
        provider="deepseek",
        model_id="deepseek:deepseek-v4-flash",
        outcome=outcome or LlmCallOutcome.SUCCESS,
        duration_ms=1,
        usage_status=UsageStatus.REPORTED,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def _unavailable(outcome=LlmCallOutcome.INVOCATION_ERROR) -> LlmCallUsageRecord:
    return LlmCallUsageRecord(
        component_name="evidence_extraction",
        provider="deepseek",
        model_id="deepseek:deepseek-v4-flash",
        outcome=outcome,
        duration_ms=1,
        usage_status=UsageStatus.UNAVAILABLE,
    )


# ---------------------------------------------------------------- collector


def test_collector_binds_identity_and_starts_empty() -> None:
    c = EvalLlmUsageCollector(
        execution_spec_fingerprint=_EXEC_FP,
        variant_id=EvalVariantId.INSIGHTFORGE_FULL,
        case_id="moutai",
    )
    assert c.execution_spec_fingerprint == _EXEC_FP
    assert c.variant_id == EvalVariantId.INSIGHTFORGE_FULL
    assert c.case_id == "moutai"
    assert c.records() == ()


@pytest.mark.asyncio
async def test_collector_records_append() -> None:
    c = EvalLlmUsageCollector(
        execution_spec_fingerprint=_EXEC_FP,
        variant_id=EvalVariantId.SINGLE_RAG,
        case_id="moutai",
    )
    await c.record(_reported())
    await c.record(_unavailable())
    records = c.records()
    assert len(records) == 2
    assert records[0].usage_status == UsageStatus.REPORTED
    assert records[1].usage_status == UsageStatus.UNAVAILABLE


def test_collectors_are_not_module_global() -> None:
    a = EvalLlmUsageCollector(
        execution_spec_fingerprint=_EXEC_FP,
        variant_id=EvalVariantId.INSIGHTFORGE_FULL,
        case_id="moutai",
    )
    b = EvalLlmUsageCollector(
        execution_spec_fingerprint=_EXEC_FP,
        variant_id=EvalVariantId.SINGLE_RAG,
        case_id="wuliangye",
    )
    assert a is not b
    assert a.records() == ()
    assert b.records() == ()


# ---------------------------------------------------------------- aggregation


def test_aggregate_zero_calls_computed_zero() -> None:
    values = aggregate_llm_usage(())
    assert set(values) == {
        MetricName.LLM_CALL_COUNT,
        MetricName.INPUT_TOKENS,
        MetricName.OUTPUT_TOKENS,
        MetricName.TOTAL_TOKENS,
    }
    assert values[MetricName.LLM_CALL_COUNT].value == Decimal(0)
    assert values[MetricName.LLM_CALL_COUNT].status == MetricStatus.COMPUTED
    # 0 call → token 指标 computed=0（无 unavailable 记录 → 不视为 incomplete）。
    assert values[MetricName.INPUT_TOKENS].status == MetricStatus.COMPUTED
    assert values[MetricName.INPUT_TOKENS].value == Decimal(0)
    assert values[MetricName.TOTAL_TOKENS].value == Decimal(0)


def test_aggregate_all_reported_sums_tokens() -> None:
    values = aggregate_llm_usage(
        (
            _reported(input_tokens=10, output_tokens=5, total_tokens=15),
            _reported(input_tokens=20, output_tokens=8, total_tokens=28),
        )
    )
    assert values[MetricName.LLM_CALL_COUNT].value == Decimal(2)
    assert values[MetricName.LLM_CALL_COUNT].sample_count == 2
    assert values[MetricName.INPUT_TOKENS].value == Decimal(30)
    assert values[MetricName.OUTPUT_TOKENS].value == Decimal(13)
    assert values[MetricName.TOTAL_TOKENS].value == Decimal(43)


def test_aggregate_counts_invocation_error_but_marks_tokens_unavailable() -> None:
    # parsing_error / invocation_error 也计入 call_count，但 token 因 usage
    # unavailable 而整体 unavailable。
    values = aggregate_llm_usage(
        (_reported(), _unavailable(outcome=LlmCallOutcome.INVOCATION_ERROR))
    )
    assert values[MetricName.LLM_CALL_COUNT].value == Decimal(2)
    for name in (MetricName.INPUT_TOKENS, MetricName.OUTPUT_TOKENS, MetricName.TOTAL_TOKENS):
        assert values[name].status == MetricStatus.UNAVAILABLE
        assert values[name].value is None
        assert values[name].reason_code == "incomplete_llm_usage"


def test_aggregate_unavailable_token_value_is_none() -> None:
    values = aggregate_llm_usage((_unavailable(),))
    # unavailable 时 value 必须为 None（MetricValue 不变量：非 computed → value None）。
    assert values[MetricName.INPUT_TOKENS].value is None
    assert values[MetricName.LLM_CALL_COUNT].value == Decimal(1)
