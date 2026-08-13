"""Evaluation execution harness (stage 7B.1.2C spec N/O/P).

`execute_variant_attempt` 编排一次 variant attempt：

1. 前置校验 `runner.variant_id == spec.variant_id`（不一致 = 装配错误，抛
   `EvalVariantError`，不属于 variant 执行失败）；
2. 创建 `EvalLlmUsageCollector` 并注入 runner（runner 必须线程到内部 LLM adapter；
   0 record = 0 LLM call）；
3. 单调时钟 `time.perf_counter_ns()` 计量 `wall_latency_ms`（**不** datetime、
   **不**把 per-call LLM duration 求和映射为 latency）；
4. `await runner.run(...)` → 校验 output variant/case identity → hard identity
   校验（duplicate id）→ 计算 output fingerprint → 返回 success / failed result；
5. 失败路径：error_code = 异常稳定 `.code` 或 `"eval_variant_execution_error"`；
   **不**保存 exception message / traceback / prompt / raw response / reasoning。
"""

from __future__ import annotations

import time
from uuid import UUID

from app.eval.bundle.loader import LoadedEvalExecutionCase
from app.eval.contracts import EvalExecutionSpec
from app.eval.errors import EvalOutputStructureError, EvalVariantError
from app.eval.execution.contracts import EvalExecutionAttemptResult, ExecutionAttemptStatus
from app.eval.execution.runner import VariantRunner
from app.eval.fingerprints import (
    compute_execution_spec_fingerprint,
    compute_variant_output_fingerprint,
)
from app.eval.scoring.context import EvalScoringContext
from app.eval.scoring.deterministic import verify_variant_output_identity
from app.eval.usage.collector import EvalLlmUsageCollector

_FALLBACK_ERROR_CODE = "eval_variant_execution_error"


def _wall_latency_ms(start_ns: int) -> int:
    return max(0, (time.perf_counter_ns() - start_ns) // 1_000_000)


def _error_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code.strip():
        return code
    return _FALLBACK_ERROR_CODE


def _verify_output(
    output,
    execution_case: LoadedEvalExecutionCase,
    execution_spec: EvalExecutionSpec,
    execution_spec_fingerprint: str,
) -> None:
    """校验 output 的 variant / case identity，再做 hard structural identity 校验。"""
    if output.variant_id != execution_spec.variant_id:
        raise EvalOutputStructureError(
            f"variant output 身份不一致：{output.variant_id.value} != "
            f"{execution_spec.variant_id.value}"
        )
    if output.case_id != execution_case.case_id:
        raise EvalOutputStructureError("variant output case_id 与 execution case 不一致")
    if output.case_version != execution_case.case_version:
        raise EvalOutputStructureError("variant output case_version 与 execution case 不一致")
    verify_variant_output_identity(
        EvalScoringContext(
            execution_spec_fingerprint=execution_spec_fingerprint,
            variant_output=output,
            source_snapshot=execution_case.snapshot,
        )
    )


async def execute_variant_attempt(
    *,
    runner: VariantRunner,
    execution_case: LoadedEvalExecutionCase,
    execution_spec: EvalExecutionSpec,
    trial_fingerprint: str,
    attempt_no: int,
    execution_id: UUID,
) -> EvalExecutionAttemptResult:
    """执行一次 variant attempt，返回冻结 success/failed result。

    `runner.variant_id != spec.variant_id` 抛 `EvalVariantError`（装配错误）；其余
    runner 异常 / output 校验失败 → failed result（error_code 取异常稳定 `.code`）。
    """
    if runner.variant_id != execution_spec.variant_id:
        raise EvalVariantError(
            f"runner variant {runner.variant_id.value} != spec variant "
            f"{execution_spec.variant_id.value}"
        )
    execution_spec_fingerprint = compute_execution_spec_fingerprint(execution_spec)
    collector = EvalLlmUsageCollector(
        execution_spec_fingerprint=execution_spec_fingerprint,
        variant_id=execution_spec.variant_id,
        case_id=execution_case.case_id,
    )
    start_ns = time.perf_counter_ns()
    try:
        output = await runner.run(execution_case, execution_spec, usage_observer=collector)
        _verify_output(output, execution_case, execution_spec, execution_spec_fingerprint)
        output_fingerprint = compute_variant_output_fingerprint(output)
    except Exception as exc:  # noqa: BLE001 — 任何 runner 异常都收敛为 failed result
        return EvalExecutionAttemptResult(
            execution_id=execution_id,
            trial_fingerprint=trial_fingerprint,
            attempt_no=attempt_no,
            variant_id=execution_spec.variant_id,
            case_id=execution_case.case_id,
            case_version=execution_case.case_version,
            status=ExecutionAttemptStatus.FAILED,
            wall_latency_ms=_wall_latency_ms(start_ns),
            variant_output=None,
            variant_output_fingerprint=None,
            usage_records=collector.records(),
            error_code=_error_code(exc),
        )
    return EvalExecutionAttemptResult(
        execution_id=execution_id,
        trial_fingerprint=trial_fingerprint,
        attempt_no=attempt_no,
        variant_id=execution_spec.variant_id,
        case_id=execution_case.case_id,
        case_version=execution_case.case_version,
        status=ExecutionAttemptStatus.SUCCESS,
        wall_latency_ms=_wall_latency_ms(start_ns),
        variant_output=output,
        variant_output_fingerprint=output_fingerprint,
        usage_records=collector.records(),
        error_code=None,
    )
