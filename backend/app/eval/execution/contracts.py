"""Evaluation execution runtime contracts (stage 7B.1.2C spec G/I/J/K).

`EvalExecutionSpec`（已冻结于 `app.eval.contracts`）描述「系统实际看到什么 + 以什么
配置运行」；在它之下冻结 **Trial → Attempt** 两层执行身份（persistence 前）：

- `EvalTrialSpec`：同一 execution spec 的一次复现变体（`schema_version` +
  `execution_spec_fingerprint` + `trial_no`）；trial fingerprint = 三者 canonical
  SHA-256，同一 spec 下 trial1 ≠ trial2（trial_no 不同）。
- `EvalExecutionAttempt`：trial 内的一次重试；`execution_id` 是 runtime UUID，
  **不**进入 semantic identity（attempt identity = `(trial_fingerprint, attempt_no)`）。
- `EvalExecutionAttemptResult`：一次 attempt 的冻结执行结果（success / failed），
  **不含** exception message / traceback / prompt / raw response / reasoning。

这些是**纯 Python frozen dataclass**（与 `LlmCallUsageRecord` 一致），不是 JSON
持久化契约；DB schema 需镜像 `ExecutionSpec 1:N Trial 1:N Attempt`（spec R）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from app.eval.canonical import canonical_json_str
from app.eval.contracts import EvalVariantOutput, _is_sha256_hex, _validate_slug
from app.eval.variants import EvalVariantId
from app.llm.instrumentation import LlmCallUsageRecord


class ExecutionAttemptStatus(StrEnum):
    """一次 attempt 的终态。"""

    SUCCESS = "success"
    FAILED = "failed"


def _require_int_ge_1(value: int, field: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{field} 必须是 >= 1 的 int，得到 {value!r}")


def _require_nonneg_int(value: int, field: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field} 必须是 >= 0 的 int，得到 {value!r}")


@dataclass(frozen=True)
class EvalTrialSpec:
    """一次 execution 的复现变体（frozen，persistence 前）。

    `schema_version=1`；同一 execution spec 下靠 `trial_no` 区分多个 trial。

    冻结语义（spec A）：trial fingerprint **不**包含 `random_seed`——当前
    `VariantRunner.run()` 拿不到 TrialSpec，生产 model config 也未真正应用 seed，
    把 seed 放进 semantic fingerprint 等于给一个不影响真实执行的字段记账。若未来
    provider 真正支持 deterministic seed，seed 必须进入 `EvalExecutionConfig` /
    `FrozenModelConfig` 并由 real runner 实际应用后，才允许进入 fingerprint。
    """

    execution_spec_fingerprint: str
    trial_no: int
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not _is_sha256_hex(self.execution_spec_fingerprint):
            raise ValueError("execution_spec_fingerprint 必须是 64 位小写 hex")
        _require_int_ge_1(self.trial_no, "trial_no")
        if self.schema_version != 1:
            raise ValueError(f"schema_version 必须为 1，得到 {self.schema_version!r}")


def compute_trial_fingerprint(trial: EvalTrialSpec) -> str:
    """trial semantic identity = schema_version + execution_spec_fingerprint + trial_no。"""
    payload = {
        "schema_version": trial.schema_version,
        "execution_spec_fingerprint": trial.execution_spec_fingerprint,
        "trial_no": trial.trial_no,
    }
    return hashlib.sha256(canonical_json_str(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EvalExecutionAttempt:
    """trial 内的一次重试。

    attempt identity = `(trial_fingerprint, attempt_no)`；`execution_id` 是 runtime
    UUID（去重 / provenance），**不**进入 semantic fingerprint。
    """

    trial_fingerprint: str
    attempt_no: int
    execution_id: UUID

    def __post_init__(self) -> None:
        if not _is_sha256_hex(self.trial_fingerprint):
            raise ValueError("trial_fingerprint 必须是 64 位小写 hex")
        _require_int_ge_1(self.attempt_no, "attempt_no")
        if not isinstance(self.execution_id, UUID):
            raise ValueError(f"execution_id 必须是 UUID，得到 {self.execution_id!r}")


@dataclass(frozen=True)
class EvalVariantRuntimeContext:
    """一次 attempt 的 runtime 身份（VariantRunner 派生隔离状态的唯一依据）。

    由 harness 从当前 `EvalExecutionAttempt` 构造并传给 `VariantRunner.run()`：
    runner 不得自造 execution_id。derived state（Chroma collection / 临时 index /
    缓存）必须绑定 `execution_id`——同一 ExecutionSpec 下 Trial1/Attempt1、
    Trial2/Attempt1、Trial2/Attempt2 是三次独立 attempt，不能共享派生状态。

    不重复携带 `execution_spec_fingerprint`：它已嵌入 `trial_fingerprint`，
    且 `EvalExecutionSpec` 本身由 runner 直接拿到。
    """

    execution_id: UUID
    trial_fingerprint: str
    attempt_no: int

    def __post_init__(self) -> None:
        if not isinstance(self.execution_id, UUID):
            raise ValueError(f"execution_id 必须是 UUID，得到 {self.execution_id!r}")
        if not _is_sha256_hex(self.trial_fingerprint):
            raise ValueError("trial_fingerprint 必须是 64 位小写 hex")
        _require_int_ge_1(self.attempt_no, "attempt_no")


@dataclass(frozen=True)
class EvalExecutionAttemptResult:
    """一次 attempt 的冻结执行结果（success / failed）。

    - success：`variant_output` + `variant_output_fingerprint` 齐备，`error_code=None`；
    - failed：`variant_output` / `variant_output_fingerprint` 为 None，`error_code` 必填。
    - **不保存** exception message / traceback / prompt / raw response / reasoning。
    """

    execution_id: UUID
    trial_fingerprint: str
    attempt_no: int
    variant_id: EvalVariantId
    case_id: str
    case_version: int
    status: ExecutionAttemptStatus
    wall_latency_ms: int
    variant_output: EvalVariantOutput | None
    variant_output_fingerprint: str | None
    usage_records: tuple[LlmCallUsageRecord, ...]
    error_code: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.execution_id, UUID):
            raise ValueError(f"execution_id 必须是 UUID，得到 {self.execution_id!r}")
        if not _is_sha256_hex(self.trial_fingerprint):
            raise ValueError("trial_fingerprint 必须是 64 位小写 hex")
        _require_int_ge_1(self.attempt_no, "attempt_no")
        _validate_slug(self.case_id)
        _require_int_ge_1(self.case_version, "case_version")
        _require_nonneg_int(self.wall_latency_ms, "wall_latency_ms")
        if self.status == ExecutionAttemptStatus.SUCCESS:
            if self.variant_output is None:
                raise ValueError("success 时 variant_output 必须存在")
            if not self.variant_output_fingerprint or not _is_sha256_hex(
                self.variant_output_fingerprint
            ):
                raise ValueError("success 时 variant_output_fingerprint 必须是 64 位小写 hex")
            if self.error_code is not None:
                raise ValueError("success 时 error_code 必须为 None")
        else:
            if self.variant_output is not None:
                raise ValueError("failed 时 variant_output 必须为 None")
            if self.variant_output_fingerprint is not None:
                raise ValueError("failed 时 variant_output_fingerprint 必须为 None")
            if not self.error_code or not self.error_code.strip():
                raise ValueError("failed 时 error_code 必须非空")
