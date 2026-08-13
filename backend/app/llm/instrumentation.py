"""LLM structured-output instrumentation (stage 7B.1.2B).

给生产 DeepSeek adapter 提供最小、可观测的 structured-output 调用包装，用于
7B.1.2B 的 token / call count 运行时遥测采集（不含 cost / latency 映射 / DB）：

- 每次调用都产生一条 `LlmCallUsageRecord`（component / provider / model /
  outcome / duration / usage token）；
- 通过 `LlmUsageObserver` Protocol 注入 observer（默认 no-op），让 usage 采集与
  eval 解耦（本模块**不**依赖 `app.eval`，也不依赖 `EvalExecutionSpec`）；
- `invoke_structured_with_usage` 用
  `with_structured_output(schema, include_raw=True)`，读取
  `result["raw"] / ["parsed"] / ["parsing_error"]`，语义与旧 adapter
  `include_raw=False` 完全一致：success 返回 parsed、parse 失败 raise 原异常、
  invoke 失败 re-raise 原异常。

冻结语义：
- `LlmCallUsageRecord` 是纯 Python frozen 契约，只保存 LangChain standardized
  usage 字段（input/output/total tokens + input/output token_details），
  **不保存** raw provider response / prompt / AIMessage.content /
  reasoning_content / tool arguments / API key。
- `usage_status=reported` → input/output/total tokens 必须完整且非负 int；
  `usage_status=unavailable` → token 字段一律 None（**不自动填 0**）。
- 每次尝试（含 parsing_error / invocation_error）都计一次 call。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class LlmCallOutcome(StrEnum):
    SUCCESS = "success"
    PARSING_ERROR = "parsing_error"
    INVOCATION_ERROR = "invocation_error"


class UsageStatus(StrEnum):
    REPORTED = "reported"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class LlmCallUsageRecord:
    """一次 LLM structured-output 调用的 usage 遥测记录（纯 Python frozen 契约）。

    只保存 LangChain standardized usage 字段；不保存 raw response / prompt /
    AIMessage.content / reasoning_content / tool args / key。
    """

    component_name: str
    provider: str
    model_id: str
    outcome: LlmCallOutcome
    duration_ms: int
    usage_status: UsageStatus
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    input_token_details: dict[str, int] | None = None
    output_token_details: dict[str, int] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.duration_ms, int) or isinstance(self.duration_ms, bool):
            raise ValueError(f"duration_ms 必须是 int，得到 {self.duration_ms!r}")
        if self.duration_ms < 0:
            raise ValueError(f"duration_ms 必须 >= 0，得到 {self.duration_ms}")
        if self.usage_status == UsageStatus.REPORTED:
            for name in ("input_tokens", "output_tokens", "total_tokens"):
                value = getattr(self, name)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise ValueError(
                        f"usage_status=reported 时 {name} 必须是非负 int，得到 {value!r}"
                    )
        else:
            if any(
                getattr(self, name) is not None
                for name in ("input_tokens", "output_tokens", "total_tokens")
            ):
                raise ValueError("usage_status=unavailable 时 token 字段必须为 None")


class LlmUsageObserver(Protocol):
    """usage record 的接收者（本模块不依赖 app.eval）。

    生产默认 no-op；eval 层注入 `EvalLlmUsageCollector`。
    """

    async def record(self, record: LlmCallUsageRecord) -> None: ...


class NullLlmUsageObserver:
    """默认 no-op observer（等价于 `usage_observer=None`）。"""

    async def record(self, record: LlmCallUsageRecord) -> None:
        return None


def _elapsed_ms(start: float) -> int:
    return max(0, int(round((time.monotonic() - start) * 1000)))


def _usage_field(usage: Any, key: str) -> Any:
    if isinstance(usage, dict):
        return usage.get(key)
    return getattr(usage, key, None)


def _extract_usage(raw: Any) -> dict[str, Any] | None:
    """从 AIMessage.usage_metadata 提取 LangChain standardized usage 字段。

    三个 token 字段必须同时存在且为非负 int 才视为 reported；任一缺失 / 非法则
    返回 None（unavailable，不自动填 0）。
    """
    usage = getattr(raw, "usage_metadata", None)
    if usage is None:
        return None
    tokens: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = _usage_field(usage, key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        tokens[key] = value
    result: dict[str, Any] = dict(tokens)
    input_details = _usage_field(usage, "input_token_details")
    output_details = _usage_field(usage, "output_token_details")
    if input_details:
        result["input_token_details"] = dict(input_details)
    if output_details:
        result["output_token_details"] = dict(output_details)
    return result


def _first_parsing_error(value: Any) -> BaseException | None:
    """`include_raw=True` 的 `parsing_error` 是单个异常；防御 future 变为 list。"""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        if not value:
            return None
        return value[0]
    return value


def _build_record(
    *,
    component_name: str,
    provider: str,
    model_id: str,
    outcome: LlmCallOutcome,
    duration_ms: int,
    usage: dict[str, Any] | None,
) -> LlmCallUsageRecord:
    if usage is None:
        return LlmCallUsageRecord(
            component_name=component_name,
            provider=provider,
            model_id=model_id,
            outcome=outcome,
            duration_ms=duration_ms,
            usage_status=UsageStatus.UNAVAILABLE,
        )
    return LlmCallUsageRecord(
        component_name=component_name,
        provider=provider,
        model_id=model_id,
        outcome=outcome,
        duration_ms=duration_ms,
        usage_status=UsageStatus.REPORTED,
        **usage,
    )


async def _record(
    observer: LlmUsageObserver | None,
    record: LlmCallUsageRecord,
) -> None:
    if observer is not None:
        await observer.record(record)


async def invoke_structured_with_usage(
    model: Any,
    schema: Any,
    input: Any,
    *,
    component_name: str,
    provider: str,
    model_id: str,
    usage_observer: LlmUsageObserver | None = None,
) -> Any:
    """调用 `model.with_structured_output(schema, include_raw=True)` 并采集 usage。

    返回 parsed（与原 adapter `include_raw=False` 语义一致）：
    - 成功：返回 `result["parsed"]`；
    - 解析失败：先 record usage（outcome=parsing_error），再 raise 原
      `result["parsing_error"]`（保持 adapter 的 OutputParserException 映射）；
    - invoke 失败：record invocation_error 后 re-raise 原异常。
    """
    structured = model.with_structured_output(schema, include_raw=True)
    start = time.monotonic()
    try:
        result = await structured.ainvoke(input)
    except Exception as exc:
        await _record(
            usage_observer,
            LlmCallUsageRecord(
                component_name=component_name,
                provider=provider,
                model_id=model_id,
                outcome=LlmCallOutcome.INVOCATION_ERROR,
                duration_ms=_elapsed_ms(start),
                usage_status=UsageStatus.UNAVAILABLE,
            ),
        )
        raise exc
    duration_ms = _elapsed_ms(start)
    parsed = result["parsed"]
    parsing_error = _first_parsing_error(result.get("parsing_error"))
    usage = _extract_usage(result["raw"])
    if parsing_error is not None:
        await _record(
            usage_observer,
            _build_record(
                component_name=component_name,
                provider=provider,
                model_id=model_id,
                outcome=LlmCallOutcome.PARSING_ERROR,
                duration_ms=duration_ms,
                usage=usage,
            ),
        )
        raise parsing_error
    await _record(
        usage_observer,
        _build_record(
            component_name=component_name,
            provider=provider,
            model_id=model_id,
            outcome=LlmCallOutcome.SUCCESS,
            duration_ms=duration_ms,
            usage=usage,
        ),
    )
    return parsed
