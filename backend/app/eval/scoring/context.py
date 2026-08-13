"""Deterministic scoring context (stage 7B.1.2A).

`EvalScoringContext` 是一次 deterministic 计量的输入：variant 执行产物
（normalized output）+ 它读取的 frozen source snapshot + 归属 execution spec
的 fingerprint。**不含** HumanLabel——human_labeled 指标属另一来源，绝不进入
deterministic 计量（MetricKind 禁止跨来源混合）。
"""

from pydantic import BaseModel, ConfigDict, field_validator

from app.eval.contracts import (
    EvalVariantOutput,
    FrozenSourceSnapshot,
    _validate_sha256,
)


class EvalScoringContext(BaseModel):
    """deterministic metric 的输入上下文（无 HumanLabel / judge / DB / LLM）。"""

    model_config = ConfigDict(frozen=True)

    execution_spec_fingerprint: str
    variant_output: EvalVariantOutput
    source_snapshot: FrozenSourceSnapshot

    @field_validator("execution_spec_fingerprint")
    @classmethod
    def _v_exec_fp(cls, v: str) -> str:
        return _validate_sha256(v, field="execution_spec_fingerprint")
