"""VariantRunner protocol (stage 7B.1.2C spec L).

一个 variant 执行器把 execution 侧输入（`LoadedEvalExecutionCase` +
`EvalExecutionSpec`）跑成 normalized `EvalVariantOutput`。**不**接收 HumanLabel /
`EvalScoringSpec`（label leakage boundary）；label 只属于 scoring 侧。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.eval.bundle.loader import LoadedEvalExecutionCase
from app.eval.contracts import EvalExecutionSpec, EvalVariantOutput
from app.eval.variants import EvalVariantId
from app.llm.instrumentation import LlmUsageObserver


@runtime_checkable
class VariantRunner(Protocol):
    """variant 执行器契约。

    - `variant_id`：该 runner 实现的 variant（harness 校验 == spec.variant_id）；
    - `run(...)`：execution_case + execution_spec → normalized output；失败抛异常
      （异常稳定 `.code` 进入 attempt result 的 `error_code`）；
    - `usage_observer` 由 harness 注入（`EvalLlmUsageCollector`），runner 必须把它
      线程到其内部全部 LLM adapter（否则 usage 计为 0 = 0 LLM call）。
    """

    variant_id: EvalVariantId

    async def run(
        self,
        execution_case: LoadedEvalExecutionCase,
        execution_spec: EvalExecutionSpec,
        *,
        usage_observer: LlmUsageObserver | None,
    ) -> EvalVariantOutput: ...
