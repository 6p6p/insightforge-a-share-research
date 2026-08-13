"""Evaluation execution runtime 测试（stage 7B.1.2C spec Q，14 cases）。

覆盖 ExecutionSpec → Trial → Attempt 三层冻结身份、`EvalExecutionAttemptResult`
success/failed 不变式，以及 `execute_variant_attempt` harness 的：
- 前置 runner.variant_id == spec.variant_id 校验；
- collector 注入 runner（0 record = 0 LLM call）；
- 输出 variant/case identity 校验；
- success / failed 收敛与 error_code 稳定映射（无 exception message 泄漏）。

全部离线：fake runner + 最小 spec/case/output，0 LLM / 0 network / 0 DB。
"""

from datetime import datetime
from uuid import UUID

import pytest

from app.eval.bundle.loader import LoadedEvalExecutionCase
from app.eval.contracts import EvalExecutionSpec, EvalVariantOutput, FrozenSourceSnapshot
from app.eval.errors import EvalOutputStructureError, EvalVariantError
from app.eval.execution.contracts import (
    EvalExecutionAttempt,
    EvalExecutionAttemptResult,
    EvalTrialSpec,
    ExecutionAttemptStatus,
    compute_trial_fingerprint,
)
from app.eval.execution.harness import execute_variant_attempt
from app.eval.fingerprints import compute_variant_output_fingerprint
from app.eval.usage.collector import EvalLlmUsageCollector
from app.eval.variants import EvalVariantId

EXEC_FP = "a" * 64
SNAP_FP = "b" * 64
CONFIG_FP = "c" * 64
TRIAL_FP = "d" * 64
CASE_ID = "test-case"
UID = UUID("00000000-0000-0000-0000-000000000001")


def _spec() -> EvalExecutionSpec:
    return EvalExecutionSpec(
        case_fingerprint=EXEC_FP,
        source_snapshot_fingerprint=SNAP_FP,
        execution_config_fingerprint=CONFIG_FP,
        variant_id=EvalVariantId.SINGLE_RAG,
    )


def _case() -> LoadedEvalExecutionCase:
    return LoadedEvalExecutionCase(
        case_fingerprint=EXEC_FP,
        case_id=CASE_ID,
        case_version=1,
        company_id=UID,
        security_code="600519",
        research_question="test question",
        analysis_as_of=datetime(2026, 8, 1, 12, 0, 0),
        tags=(),
        snapshot=FrozenSourceSnapshot(),
    )


def _output() -> EvalVariantOutput:
    return EvalVariantOutput(
        variant_id=EvalVariantId.SINGLE_RAG,
        case_id=CASE_ID,
        case_version=1,
        final_text="test final text",
    )


class _FakeRunner:
    """最小 fake runner（不用 EvalVariantId.NOOP，只用现有 SINGLE_RAG）。"""

    def __init__(
        self,
        variant_id: EvalVariantId,
        output: EvalVariantOutput | None = None,
        exc: BaseException | None = None,
    ) -> None:
        self.variant_id = variant_id
        self._output = output
        self._exc = exc
        self.seen_observer = None

    async def run(self, execution_case, execution_spec, *, usage_observer=None):
        self.seen_observer = usage_observer
        if self._exc is not None:
            raise self._exc
        assert self._output is not None
        return self._output


# ---------------------------------------------------------------- trial spec


def test_trial_spec_valid_and_defaults() -> None:
    trial = EvalTrialSpec(execution_spec_fingerprint=EXEC_FP, trial_no=1)
    assert trial.schema_version == 1
    assert trial.random_seed is None
    seeded = EvalTrialSpec(execution_spec_fingerprint=EXEC_FP, trial_no=2, random_seed=42)
    assert seeded.random_seed == 42


def test_trial_spec_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        EvalTrialSpec(execution_spec_fingerprint=EXEC_FP, trial_no=0)
    with pytest.raises(ValueError):
        EvalTrialSpec(execution_spec_fingerprint="not-hex", trial_no=1)
    with pytest.raises(ValueError):
        EvalTrialSpec(execution_spec_fingerprint=EXEC_FP, trial_no=1, random_seed="x")


def test_trial_fingerprint_deterministic_and_distinct() -> None:
    t1 = EvalTrialSpec(execution_spec_fingerprint=EXEC_FP, trial_no=1)
    t1_again = EvalTrialSpec(execution_spec_fingerprint=EXEC_FP, trial_no=1)
    t2 = EvalTrialSpec(execution_spec_fingerprint=EXEC_FP, trial_no=2)
    seeded = EvalTrialSpec(execution_spec_fingerprint=EXEC_FP, trial_no=1, random_seed=0)

    assert compute_trial_fingerprint(t1) == compute_trial_fingerprint(t1_again)
    # 同一 spec 下 trial1 ≠ trial2
    assert compute_trial_fingerprint(t1) != compute_trial_fingerprint(t2)
    # random_seed=None（null）与 0 不同
    assert compute_trial_fingerprint(t1) != compute_trial_fingerprint(seeded)


# ---------------------------------------------------------------- attempt


def test_attempt_identity_and_validation() -> None:
    attempt = EvalExecutionAttempt(trial_fingerprint=TRIAL_FP, attempt_no=1, execution_id=UID)
    # attempt identity = (trial_fingerprint, attempt_no)；execution_id 不进 semantic identity
    assert (attempt.trial_fingerprint, attempt.attempt_no) == (TRIAL_FP, 1)
    assert attempt.execution_id == UID
    with pytest.raises(ValueError):
        EvalExecutionAttempt(trial_fingerprint=TRIAL_FP, attempt_no=0, execution_id=UID)
    with pytest.raises(ValueError):
        EvalExecutionAttempt(trial_fingerprint="x", attempt_no=1, execution_id=UID)


def test_attempt_status_enum() -> None:
    assert ExecutionAttemptStatus.SUCCESS.value == "success"
    assert ExecutionAttemptStatus.FAILED.value == "failed"


# ---------------------------------------------------------------- attempt result


def test_result_success_invariant() -> None:
    output = _output()
    fp = compute_variant_output_fingerprint(output)
    result = EvalExecutionAttemptResult(
        execution_id=UID,
        trial_fingerprint=TRIAL_FP,
        attempt_no=1,
        variant_id=EvalVariantId.SINGLE_RAG,
        case_id=CASE_ID,
        case_version=1,
        status=ExecutionAttemptStatus.SUCCESS,
        wall_latency_ms=5,
        variant_output=output,
        variant_output_fingerprint=fp,
        usage_records=(),
        error_code=None,
    )
    assert result.variant_output is output
    assert result.variant_output_fingerprint == fp

    with pytest.raises(ValueError):
        # success 时 error_code 必须为 None
        EvalExecutionAttemptResult(
            execution_id=UID,
            trial_fingerprint=TRIAL_FP,
            attempt_no=1,
            variant_id=EvalVariantId.SINGLE_RAG,
            case_id=CASE_ID,
            case_version=1,
            status=ExecutionAttemptStatus.SUCCESS,
            wall_latency_ms=5,
            variant_output=output,
            variant_output_fingerprint=fp,
            usage_records=(),
            error_code="unexpected",
        )
    with pytest.raises(ValueError):
        # success 时 output 必须存在
        EvalExecutionAttemptResult(
            execution_id=UID,
            trial_fingerprint=TRIAL_FP,
            attempt_no=1,
            variant_id=EvalVariantId.SINGLE_RAG,
            case_id=CASE_ID,
            case_version=1,
            status=ExecutionAttemptStatus.SUCCESS,
            wall_latency_ms=5,
            variant_output=None,
            variant_output_fingerprint=None,
            usage_records=(),
            error_code=None,
        )


def test_result_failed_invariant() -> None:
    result = EvalExecutionAttemptResult(
        execution_id=UID,
        trial_fingerprint=TRIAL_FP,
        attempt_no=1,
        variant_id=EvalVariantId.SINGLE_RAG,
        case_id=CASE_ID,
        case_version=1,
        status=ExecutionAttemptStatus.FAILED,
        wall_latency_ms=5,
        variant_output=None,
        variant_output_fingerprint=None,
        usage_records=(),
        error_code="eval_variant_execution_error",
    )
    assert result.variant_output is None
    assert result.variant_output_fingerprint is None
    assert result.error_code == "eval_variant_execution_error"

    with pytest.raises(ValueError):
        # failed 时 output 必须为 None
        EvalExecutionAttemptResult(
            execution_id=UID,
            trial_fingerprint=TRIAL_FP,
            attempt_no=1,
            variant_id=EvalVariantId.SINGLE_RAG,
            case_id=CASE_ID,
            case_version=1,
            status=ExecutionAttemptStatus.FAILED,
            wall_latency_ms=5,
            variant_output=_output(),
            variant_output_fingerprint="e" * 64,
            usage_records=(),
            error_code="eval_variant_execution_error",
        )
    with pytest.raises(ValueError):
        # failed 时 error_code 必须非空
        EvalExecutionAttemptResult(
            execution_id=UID,
            trial_fingerprint=TRIAL_FP,
            attempt_no=1,
            variant_id=EvalVariantId.SINGLE_RAG,
            case_id=CASE_ID,
            case_version=1,
            status=ExecutionAttemptStatus.FAILED,
            wall_latency_ms=5,
            variant_output=None,
            variant_output_fingerprint=None,
            usage_records=(),
            error_code=None,
        )


# ---------------------------------------------------------------- harness


@pytest.mark.asyncio
async def test_harness_success_returns_result() -> None:
    spec = _spec()
    case = _case()
    output = _output()
    runner = _FakeRunner(EvalVariantId.SINGLE_RAG, output=output)

    result = await execute_variant_attempt(
        runner=runner,
        execution_case=case,
        execution_spec=spec,
        trial_fingerprint=TRIAL_FP,
        attempt_no=1,
        execution_id=UID,
    )

    assert result.status == ExecutionAttemptStatus.SUCCESS
    assert result.variant_output is output
    assert result.variant_output_fingerprint == compute_variant_output_fingerprint(output)
    assert result.variant_id == EvalVariantId.SINGLE_RAG
    assert result.case_id == CASE_ID
    assert result.case_version == 1
    assert isinstance(result.wall_latency_ms, int) and result.wall_latency_ms >= 0
    assert result.error_code is None
    assert result.usage_records == ()


@pytest.mark.asyncio
async def test_harness_failure_eval_error_code() -> None:
    runner = _FakeRunner(
        EvalVariantId.SINGLE_RAG, exc=EvalOutputStructureError("duplicate claim_id")
    )
    result = await execute_variant_attempt(
        runner=runner,
        execution_case=_case(),
        execution_spec=_spec(),
        trial_fingerprint=TRIAL_FP,
        attempt_no=1,
        execution_id=UID,
    )
    assert result.status == ExecutionAttemptStatus.FAILED
    assert result.error_code == "eval_output_structure_error"
    assert result.variant_output is None
    assert result.variant_output_fingerprint is None


@pytest.mark.asyncio
async def test_harness_failure_plain_exception() -> None:
    runner = _FakeRunner(EvalVariantId.SINGLE_RAG, exc=ValueError("boom"))
    result = await execute_variant_attempt(
        runner=runner,
        execution_case=_case(),
        execution_spec=_spec(),
        trial_fingerprint=TRIAL_FP,
        attempt_no=1,
        execution_id=UID,
    )
    assert result.status == ExecutionAttemptStatus.FAILED
    assert result.error_code == "eval_variant_execution_error"


@pytest.mark.asyncio
async def test_harness_output_identity_mismatch() -> None:
    bad = EvalVariantOutput(
        variant_id=EvalVariantId.SINGLE_RAG,
        case_id="other-case",
        case_version=1,
        final_text="test final text",
    )
    runner = _FakeRunner(EvalVariantId.SINGLE_RAG, output=bad)
    result = await execute_variant_attempt(
        runner=runner,
        execution_case=_case(),
        execution_spec=_spec(),
        trial_fingerprint=TRIAL_FP,
        attempt_no=1,
        execution_id=UID,
    )
    assert result.status == ExecutionAttemptStatus.FAILED
    assert result.error_code == "eval_output_structure_error"


@pytest.mark.asyncio
async def test_harness_runner_variant_mismatch_raises() -> None:
    runner = _FakeRunner(EvalVariantId.MULTI_STAGE_NO_AUDIT, output=_output())
    with pytest.raises(EvalVariantError):
        await execute_variant_attempt(
            runner=runner,
            execution_case=_case(),
            execution_spec=_spec(),
            trial_fingerprint=TRIAL_FP,
            attempt_no=1,
            execution_id=UID,
        )


@pytest.mark.asyncio
async def test_harness_passes_collector_to_runner() -> None:
    runner = _FakeRunner(EvalVariantId.SINGLE_RAG, output=_output())
    await execute_variant_attempt(
        runner=runner,
        execution_case=_case(),
        execution_spec=_spec(),
        trial_fingerprint=TRIAL_FP,
        attempt_no=1,
        execution_id=UID,
    )
    assert runner.seen_observer is not None
    assert isinstance(runner.seen_observer, EvalLlmUsageCollector)


@pytest.mark.asyncio
async def test_harness_zero_llm_calls_zero_records() -> None:
    runner = _FakeRunner(EvalVariantId.SINGLE_RAG, output=_output())
    result = await execute_variant_attempt(
        runner=runner,
        execution_case=_case(),
        execution_spec=_spec(),
        trial_fingerprint=TRIAL_FP,
        attempt_no=1,
        execution_id=UID,
    )
    # fake runner 未调用任何 LLM / 未线程 observer → 0 record
    assert result.usage_records == ()
