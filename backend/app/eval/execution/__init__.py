"""Evaluation execution runtime (stage 7B.1.2C).

ExecutionSpec → Trial → Attempt 三层冻结身份 + `VariantRunner` + `execute_variant_attempt`
execution harness。纯 Python；0 DB / 0 LLM / 0 network（不含真实 variant runner）。
"""

from app.eval.execution.contracts import (
    EvalExecutionAttempt,
    EvalExecutionAttemptResult,
    EvalTrialSpec,
    ExecutionAttemptStatus,
    compute_trial_fingerprint,
)
from app.eval.execution.harness import execute_variant_attempt
from app.eval.execution.runner import VariantRunner

__all__ = [
    "EvalExecutionAttempt",
    "EvalExecutionAttemptResult",
    "EvalTrialSpec",
    "ExecutionAttemptStatus",
    "VariantRunner",
    "compute_trial_fingerprint",
    "execute_variant_attempt",
]
