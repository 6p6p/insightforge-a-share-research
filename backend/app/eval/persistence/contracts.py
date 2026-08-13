"""Evaluation execution persistence read models (stage 7B.1.3A).

`verify_*_integrity` 的**verified read model**：从 DB 行加载 → `model_validate`
（**不**用 `model_construct`，绝不相信 DB JSONB）→ 重算 fingerprint → 校验一致后
返回。每个 read model 只携带「已验证」的字段，不含 DB 的 `created_at` / 原始
JSONB payload（payload 已投影为 typed contract）。

- `VerifiedExecutionSpecRecord`：spec + config（均从 payload 重校验）。
- `VerifiedTrialRecord`：trial_spec（从 payload 重校验）。
- `VerifiedAttemptRecord`：attempt 结果 + usage records（call_index 排序、连续）。
"""

from dataclasses import dataclass
from uuid import UUID

from app.eval.contracts import EvalExecutionConfig, EvalExecutionSpec, EvalVariantOutput
from app.eval.execution.contracts import EvalTrialSpec, ExecutionAttemptStatus
from app.llm.instrumentation import LlmCallUsageRecord


@dataclass(frozen=True)
class VerifiedExecutionSpecRecord:
    """一次 execution spec 的已验证读模型（spec + config 均重校验）。"""

    execution_spec_id: UUID
    spec: EvalExecutionSpec
    config: EvalExecutionConfig


@dataclass(frozen=True)
class VerifiedTrialRecord:
    """一次 trial 的已验证读模型。"""

    trial_id: UUID
    execution_spec_id: UUID
    trial_spec: EvalTrialSpec


@dataclass(frozen=True)
class VerifiedAttemptRecord:
    """一次 attempt 的已验证读模型（含排序后的 usage records）。"""

    execution_id: UUID
    trial_id: UUID
    attempt_no: int
    status: ExecutionAttemptStatus
    wall_latency_ms: int
    variant_output: EvalVariantOutput | None
    variant_output_fingerprint: str | None
    error_code: str | None
    usage_records: tuple[LlmCallUsageRecord, ...]
