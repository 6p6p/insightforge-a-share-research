"""Per-execution LLM usage collector (stage 7B.1.2B).

`EvalLlmUsageCollector` 实现 `LlmUsageObserver`，把某次 variant execution 的
所有 LLM call 收集到内存。它**不**用 module global list，可安全地在 LangGraph
并行 worker 间共享：`record()` 是同步 append（无内部 await），在单事件循环内
原子完成，天然避免数据竞争。
"""

from __future__ import annotations

from app.eval.variants import EvalVariantId
from app.llm.instrumentation import LlmCallUsageRecord


class EvalLlmUsageCollector:
    """绑定到一次 (execution_spec_fingerprint, variant_id, case_id) 的 usage 收集器。

    同一 collector 实例被该次 execution 的所有 worker 共享；聚合只按 record 集合
    求和，不依赖调用顺序。
    """

    def __init__(
        self,
        *,
        execution_spec_fingerprint: str,
        variant_id: EvalVariantId,
        case_id: str,
    ) -> None:
        self._execution_spec_fingerprint = execution_spec_fingerprint
        self._variant_id = variant_id
        self._case_id = case_id
        self._records: list[LlmCallUsageRecord] = []

    @property
    def execution_spec_fingerprint(self) -> str:
        return self._execution_spec_fingerprint

    @property
    def variant_id(self) -> EvalVariantId:
        return self._variant_id

    @property
    def case_id(self) -> str:
        return self._case_id

    async def record(self, record: LlmCallUsageRecord) -> None:
        # 同步 append（无内部 await）：在单事件循环内原子，支持并行 worker 共享。
        self._records.append(record)

    def records(self) -> tuple[LlmCallUsageRecord, ...]:
        return tuple(self._records)
