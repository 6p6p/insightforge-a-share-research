"""Evaluation execution harness (stage 7B.1.2C spec B/C/D/E/N/O/P).

`execute_variant_attempt` 编排一次 variant attempt，分两段：

**assembly preflight（fail-fast，runner 0 calls，抛异常而非 failed result）**：
1. `trial_spec.execution_spec_fingerprint == compute_execution_spec_fingerprint(spec)`
   （spec↔trial 一致）；
2. `compute_trial_fingerprint(trial_spec) == attempt.trial_fingerprint`
   （trial↔attempt 一致）；
3. `spec.case_fingerprint == execution_case.case_fingerprint` 且
   `spec.source_snapshot_fingerprint == compute_source_snapshot_fingerprint(case.snapshot)`
   （spec↔case↔snapshot 一致）；
4. `runner.variant_id == spec.variant_id`（runner 装配正确）。

任一失败 = benchmark assembly corruption，抛 `EvalExecutionAssemblyError`（1-3）或
`EvalVariantError`（4），**不**记为 variant execution failure。

**runtime（preflight 全过后才开始 `perf_counter_ns`）**：
5. 创建 `EvalLlmUsageCollector` 并注入 runner（0 record = 0 LLM call）；
6. `await runner.run(...)` → 校验 output variant/case identity → hard identity
   校验（duplicate id）→ 计算 output fingerprint → success / failed result；
7. 失败路径：error_code = 异常稳定 `.code` 或 `"eval_variant_execution_error"`；
   **不**保存 exception message / traceback / prompt / raw response / reasoning。

注意：output case/variant identity mismatch 发生在 runner **已执行之后**，因此收敛
为 failed `EvalExecutionAttemptResult`（actual attempt 输出问题），保持 7B.1.2C
原语义。
"""

from __future__ import annotations

import time

from app.eval.bundle.loader import LoadedEvalExecutionCase
from app.eval.contracts import EvalExecutionSpec
from app.eval.errors import (
    EvalExecutionAssemblyError,
    EvalOutputStructureError,
    EvalVariantError,
)
from app.eval.execution.contracts import (
    EvalExecutionAttempt,
    EvalExecutionAttemptResult,
    EvalTrialSpec,
    EvalVariantRuntimeContext,
    ExecutionAttemptStatus,
    compute_trial_fingerprint,
)
from app.eval.execution.runner import VariantRunner
from app.eval.fingerprints import (
    compute_execution_spec_fingerprint,
    compute_source_snapshot_fingerprint,
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
    attempt: EvalExecutionAttempt,
    trial_spec: EvalTrialSpec,
    execution_spec: EvalExecutionSpec,
    execution_case: LoadedEvalExecutionCase,
    runner: VariantRunner,
) -> EvalExecutionAttemptResult:
    """执行一次 variant attempt，返回冻结 success/failed result。

    assembly preflight 任一失败 → 抛 `EvalExecutionAssemblyError` /
    `EvalVariantError`（runner 0 calls）；只有 runner 执行 / 输出校验失败才收敛为
    failed `EvalExecutionAttemptResult`。
    """
    # ---- assembly preflight（benchmark corruption，fail-fast，不记 failed result）----
    execution_spec_fingerprint = compute_execution_spec_fingerprint(execution_spec)
    if trial_spec.execution_spec_fingerprint != execution_spec_fingerprint:
        raise EvalExecutionAssemblyError(
            "trial_spec.execution_spec_fingerprint 与 execution_spec fingerprint 不一致"
        )
    if compute_trial_fingerprint(trial_spec) != attempt.trial_fingerprint:
        raise EvalExecutionAssemblyError(
            "compute_trial_fingerprint(trial_spec) 与 attempt.trial_fingerprint 不一致"
        )
    if execution_spec.case_fingerprint != execution_case.case_fingerprint:
        raise EvalExecutionAssemblyError(
            "execution_spec.case_fingerprint 与 execution_case.case_fingerprint 不一致"
        )
    snapshot_fingerprint = compute_source_snapshot_fingerprint(execution_case.snapshot)
    if execution_spec.source_snapshot_fingerprint != snapshot_fingerprint:
        raise EvalExecutionAssemblyError(
            "execution_spec.source_snapshot_fingerprint 与 execution_case.snapshot 不一致"
        )
    if runner.variant_id != execution_spec.variant_id:
        raise EvalVariantError(
            f"runner variant {runner.variant_id.value} != spec variant "
            f"{execution_spec.variant_id.value}"
        )

    # ---- runtime 开始 ----
    collector = EvalLlmUsageCollector(
        execution_spec_fingerprint=execution_spec_fingerprint,
        variant_id=execution_spec.variant_id,
        case_id=execution_case.case_id,
    )
    trial_fingerprint = attempt.trial_fingerprint
    attempt_no = attempt.attempt_no
    execution_id = attempt.execution_id
    runtime_context = EvalVariantRuntimeContext(
        execution_id=execution_id,
        trial_fingerprint=trial_fingerprint,
        attempt_no=attempt_no,
    )
    start_ns = time.perf_counter_ns()
    try:
        output = await runner.run(
            execution_case,
            execution_spec,
            runtime_context=runtime_context,
            usage_observer=collector,
        )
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
