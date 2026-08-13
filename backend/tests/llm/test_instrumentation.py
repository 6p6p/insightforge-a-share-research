"""LLM structured-output instrumentation 单测 (stage 7B.1.2B).

零真实 DeepSeek / 零网络：用 duck-typed fake model（`with_structured_output` +
`ainvoke` 返回 `{"raw"/"parsed"/"parsing_error"}`）验证 wrapper 的 usage 采集与
异常语义，以及 `LlmCallUsageRecord` 的 frozen 契约不变量。
"""

from types import SimpleNamespace

import pytest

from app.llm.instrumentation import (
    LlmCallOutcome,
    LlmCallUsageRecord,
    NullLlmUsageObserver,
    UsageStatus,
    _extract_usage,
    invoke_structured_with_usage,
)


def _raw(**usage) -> SimpleNamespace:
    return SimpleNamespace(usage_metadata=usage)


class _FakeStructured:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    async def ainvoke(self, input):
        if self._error is not None:
            raise self._error
        return self._result


class _FakeModel:
    def __init__(self, structured):
        self._structured = structured
        self.include_raw = None

    def with_structured_output(self, schema, include_raw=True):
        self.include_raw = include_raw
        return self._structured


class _Observer:
    def __init__(self):
        self.records = []

    async def record(self, record):
        self.records.append(record)


def _call(model, *, observer=None, **kwargs):
    return invoke_structured_with_usage(
        model,
        object(),
        [{"role": "user", "content": "hi"}],
        component_name="test_component",
        provider="deepseek",
        model_id="deepseek:deepseek-v4-flash",
        usage_observer=observer,
        **kwargs,
    )


def _success_result(parsed="OK", raw=None):
    return {"raw": raw, "parsed": parsed, "parsing_error": None}


# ---------------------------------------------------------------- success


@pytest.mark.asyncio
async def test_success_with_usage_reported() -> None:
    observer = _Observer()
    model = _FakeModel(
        _FakeStructured(
            result=_success_result(raw=_raw(input_tokens=10, output_tokens=5, total_tokens=15))
        )
    )
    parsed = await _call(model, observer=observer)
    assert parsed == "OK"
    assert model.include_raw is True
    assert len(observer.records) == 1
    rec = observer.records[0]
    assert rec.outcome == LlmCallOutcome.SUCCESS
    assert rec.usage_status == UsageStatus.REPORTED
    assert rec.input_tokens == 10
    assert rec.output_tokens == 5
    assert rec.total_tokens == 15
    assert rec.duration_ms >= 0


@pytest.mark.asyncio
async def test_success_without_usage_unavailable() -> None:
    observer = _Observer()
    model = _FakeModel(_FakeStructured(result=_success_result(raw=SimpleNamespace())))
    parsed = await _call(model, observer=observer)
    assert parsed == "OK"
    rec = observer.records[0]
    assert rec.outcome == LlmCallOutcome.SUCCESS
    assert rec.usage_status == UsageStatus.UNAVAILABLE
    assert rec.input_tokens is None
    assert rec.output_tokens is None
    assert rec.total_tokens is None


@pytest.mark.asyncio
async def test_token_details_captured() -> None:
    observer = _Observer()
    raw = _raw(
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        input_token_details={"cache_read": 3},
        output_token_details={"reasoning": 2},
    )
    model = _FakeModel(_FakeStructured(result=_success_result(raw=raw)))
    await _call(model, observer=observer)
    rec = observer.records[0]
    assert rec.input_token_details == {"cache_read": 3}
    assert rec.output_token_details == {"reasoning": 2}


@pytest.mark.asyncio
async def test_observer_none_is_noop() -> None:
    model = _FakeModel(_FakeStructured(result=_success_result(raw=SimpleNamespace())))
    parsed = await _call(model, observer=None)
    assert parsed == "OK"


@pytest.mark.asyncio
async def test_null_observer_record_is_noop() -> None:
    # NullLlmUsageObserver 是 no-op，构造 record 时也不抛。
    await NullLlmUsageObserver().record(
        LlmCallUsageRecord(
            component_name="x",
            provider="deepseek",
            model_id="m",
            outcome=LlmCallOutcome.SUCCESS,
            duration_ms=0,
            usage_status=UsageStatus.UNAVAILABLE,
        )
    )


# ---------------------------------------------------------------- parsing error


@pytest.mark.asyncio
async def test_parsing_error_raises_and_records_usage() -> None:
    observer = _Observer()
    err = ValueError("bad parse")
    raw = _raw(input_tokens=10, output_tokens=5, total_tokens=15)
    model = _FakeModel(_FakeStructured(result={"raw": raw, "parsed": None, "parsing_error": err}))
    with pytest.raises(ValueError, match="bad parse"):
        await _call(model, observer=observer)
    rec = observer.records[0]
    assert rec.outcome == LlmCallOutcome.PARSING_ERROR
    assert rec.usage_status == UsageStatus.REPORTED
    assert rec.input_tokens == 10


@pytest.mark.asyncio
async def test_parsing_error_without_usage_unavailable() -> None:
    observer = _Observer()
    err = ValueError("bad parse")
    model = _FakeModel(
        _FakeStructured(result={"raw": SimpleNamespace(), "parsed": None, "parsing_error": err})
    )
    with pytest.raises(ValueError, match="bad parse"):
        await _call(model, observer=observer)
    rec = observer.records[0]
    assert rec.outcome == LlmCallOutcome.PARSING_ERROR
    assert rec.usage_status == UsageStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_parsing_error_list_takes_first() -> None:
    # 防御性：parsing_error 若为 list（future 版本）取首项。
    observer = _Observer()
    model = _FakeModel(
        _FakeStructured(
            result={
                "raw": SimpleNamespace(),
                "parsed": None,
                "parsing_error": [ValueError("first"), ValueError("second")],
            }
        )
    )
    with pytest.raises(ValueError, match="first"):
        await _call(model, observer=observer)
    assert observer.records[0].outcome == LlmCallOutcome.PARSING_ERROR


# ---------------------------------------------------------------- invocation error


@pytest.mark.asyncio
async def test_invocation_error_re_raises_and_records() -> None:
    observer = _Observer()
    model = _FakeModel(_FakeStructured(error=RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        await _call(model, observer=observer)
    rec = observer.records[0]
    assert rec.outcome == LlmCallOutcome.INVOCATION_ERROR
    assert rec.usage_status == UsageStatus.UNAVAILABLE
    assert rec.input_tokens is None


# ---------------------------------------------------------------- record contract


def test_record_reported_requires_complete_tokens() -> None:
    with pytest.raises(ValueError):
        LlmCallUsageRecord(
            component_name="x",
            provider="deepseek",
            model_id="m",
            outcome=LlmCallOutcome.SUCCESS,
            duration_ms=0,
            usage_status=UsageStatus.REPORTED,
            input_tokens=10,
            # 缺 output_tokens / total_tokens → 非法。
        )


def test_record_reported_rejects_negative_tokens() -> None:
    with pytest.raises(ValueError):
        LlmCallUsageRecord(
            component_name="x",
            provider="deepseek",
            model_id="m",
            outcome=LlmCallOutcome.SUCCESS,
            duration_ms=0,
            usage_status=UsageStatus.REPORTED,
            input_tokens=-1,
            output_tokens=0,
            total_tokens=0,
        )


def test_record_unavailable_requires_none_tokens() -> None:
    with pytest.raises(ValueError):
        LlmCallUsageRecord(
            component_name="x",
            provider="deepseek",
            model_id="m",
            outcome=LlmCallOutcome.INVOCATION_ERROR,
            duration_ms=0,
            usage_status=UsageStatus.UNAVAILABLE,
            input_tokens=10,
        )


def test_record_rejects_negative_duration() -> None:
    with pytest.raises(ValueError):
        LlmCallUsageRecord(
            component_name="x",
            provider="deepseek",
            model_id="m",
            outcome=LlmCallOutcome.SUCCESS,
            duration_ms=-1,
            usage_status=UsageStatus.UNAVAILABLE,
        )


def test_extract_usage_incomplete_returns_none() -> None:
    # 三个 token 字段不完整 → None（unavailable，不自动填 0）。
    assert _extract_usage(_raw(input_tokens=10)) is None
    assert _extract_usage(_raw(input_tokens=10, output_tokens=5)) is None
    assert _extract_usage(SimpleNamespace()) is None
    # 非 int / 负数 token → None。
    assert _extract_usage(_raw(input_tokens="10", output_tokens=5, total_tokens=15)) is None
    assert _extract_usage(_raw(input_tokens=-1, output_tokens=5, total_tokens=15)) is None
