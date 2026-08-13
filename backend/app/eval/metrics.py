"""Evaluation metric contracts (stage 7B.1.0).

冻结三路评估的指标 surface（`MetricName` / `MetricSpec` / `METRIC_SPECS`）与一次
测量的值对象 `MetricValue`。

冻结语义：
- 指标来源三分（`MetricKind`）：`deterministic`（代码算出）/ `human_labeled`
  （结构化人工标注）/ `semantic_judge`（独立 judge 模型）/ `runtime`（运行时
  遥测：token / latency / call count / cost）。比较不跨来源混合。
- 指标状态四分（`MetricStatus`）：`computed` / `not_applicable` / `unavailable` /
  `error`。
- `METRIC_SPECS` 是唯一指标注册表，`keys == set(MetricName)`（测试断言）。
- `MetricValue` 不变量：`status=computed → value 非 None`；`status≠computed →
  value None`；`denominator=0` 非法（「无 eligible 样本」用 `not_applicable`
  表达，**不自动生成 0 分**）。
"""

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class MetricDimension(StrEnum):
    CONTENT_QUALITY = "content_quality"
    RELIABILITY = "reliability"
    EFFICIENCY = "efficiency"


class MetricKind(StrEnum):
    DETERMINISTIC = "deterministic"
    HUMAN_LABELED = "human_labeled"
    SEMANTIC_JUDGE = "semantic_judge"
    RUNTIME = "runtime"


class MetricStatus(StrEnum):
    COMPUTED = "computed"
    NOT_APPLICABLE = "not_applicable"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


class MetricName(StrEnum):
    # content_quality
    FINANCIAL_ACCURACY = "financial_accuracy"
    CITATION_VALIDITY = "citation_validity"
    CITATION_COVERAGE = "citation_coverage"
    CLAIM_SUPPORT_RATE = "claim_support_rate"
    UNSUPPORTED_CLAIM_RATIO = "unsupported_claim_ratio"
    RISK_TOPIC_RECALL = "risk_topic_recall"
    MACRO_CAUSAL_ERROR_RATE = "macro_causal_error_rate"
    CONFLICT_PRESERVATION = "conflict_preservation"
    OVERCLAIM_RATE = "overclaim_rate"
    # reliability
    COMPLETION_RATE = "completion_rate"
    NODE_FAILURE_RATE = "node_failure_rate"
    RETRY_COUNT = "retry_count"
    RECOVERY_SUCCESS_RATE = "recovery_success_rate"
    DUPLICATE_WRITE_RATE = "duplicate_write_rate"
    HUMAN_RESUME_SUCCESS_RATE = "human_resume_success_rate"
    RESEARCH_BACKFLOW_SUCCESS_RATE = "research_backflow_success_rate"
    # efficiency
    LATENCY_MS = "latency_ms"
    LLM_CALL_COUNT = "llm_call_count"
    INPUT_TOKENS = "input_tokens"
    OUTPUT_TOKENS = "output_tokens"
    TOTAL_TOKENS = "total_tokens"
    ESTIMATED_COST = "estimated_cost"


class MetricSpec(BaseModel):
    """一个 `MetricName` 的冻结规格（`METRIC_SPECS` 注册表条目）。"""

    model_config = ConfigDict(frozen=True)

    name: MetricName
    dimension: MetricDimension
    kind: MetricKind
    metric_version: int = Field(default=1, ge=1)
    higher_is_better: bool


_SPEC_TABLE: tuple[tuple[MetricName, MetricDimension, MetricKind, bool], ...] = (
    # content_quality
    (
        MetricName.FINANCIAL_ACCURACY,
        MetricDimension.CONTENT_QUALITY,
        MetricKind.HUMAN_LABELED,
        True,
    ),
    (MetricName.CITATION_VALIDITY, MetricDimension.CONTENT_QUALITY, MetricKind.DETERMINISTIC, True),
    (MetricName.CITATION_COVERAGE, MetricDimension.CONTENT_QUALITY, MetricKind.DETERMINISTIC, True),
    (
        MetricName.CLAIM_SUPPORT_RATE,
        MetricDimension.CONTENT_QUALITY,
        MetricKind.DETERMINISTIC,
        True,
    ),
    (
        MetricName.UNSUPPORTED_CLAIM_RATIO,
        MetricDimension.CONTENT_QUALITY,
        MetricKind.DETERMINISTIC,
        False,
    ),
    (MetricName.RISK_TOPIC_RECALL, MetricDimension.CONTENT_QUALITY, MetricKind.HUMAN_LABELED, True),
    (
        MetricName.MACRO_CAUSAL_ERROR_RATE,
        MetricDimension.CONTENT_QUALITY,
        MetricKind.HUMAN_LABELED,
        False,
    ),
    (
        MetricName.CONFLICT_PRESERVATION,
        MetricDimension.CONTENT_QUALITY,
        MetricKind.DETERMINISTIC,
        True,
    ),
    (MetricName.OVERCLAIM_RATE, MetricDimension.CONTENT_QUALITY, MetricKind.SEMANTIC_JUDGE, False),
    # reliability
    (MetricName.COMPLETION_RATE, MetricDimension.RELIABILITY, MetricKind.DETERMINISTIC, True),
    (MetricName.NODE_FAILURE_RATE, MetricDimension.RELIABILITY, MetricKind.DETERMINISTIC, False),
    (MetricName.RETRY_COUNT, MetricDimension.RELIABILITY, MetricKind.RUNTIME, False),
    (MetricName.RECOVERY_SUCCESS_RATE, MetricDimension.RELIABILITY, MetricKind.DETERMINISTIC, True),
    (MetricName.DUPLICATE_WRITE_RATE, MetricDimension.RELIABILITY, MetricKind.DETERMINISTIC, False),
    (
        MetricName.HUMAN_RESUME_SUCCESS_RATE,
        MetricDimension.RELIABILITY,
        MetricKind.DETERMINISTIC,
        True,
    ),
    (
        MetricName.RESEARCH_BACKFLOW_SUCCESS_RATE,
        MetricDimension.RELIABILITY,
        MetricKind.DETERMINISTIC,
        True,
    ),
    # efficiency
    (MetricName.LATENCY_MS, MetricDimension.EFFICIENCY, MetricKind.RUNTIME, False),
    (MetricName.LLM_CALL_COUNT, MetricDimension.EFFICIENCY, MetricKind.RUNTIME, False),
    (MetricName.INPUT_TOKENS, MetricDimension.EFFICIENCY, MetricKind.RUNTIME, False),
    (MetricName.OUTPUT_TOKENS, MetricDimension.EFFICIENCY, MetricKind.RUNTIME, False),
    (MetricName.TOTAL_TOKENS, MetricDimension.EFFICIENCY, MetricKind.RUNTIME, False),
    (MetricName.ESTIMATED_COST, MetricDimension.EFFICIENCY, MetricKind.RUNTIME, False),
)

METRIC_SPECS: dict[MetricName, MetricSpec] = {
    name: MetricSpec(
        name=name,
        dimension=dimension,
        kind=kind,
        metric_version=1,
        higher_is_better=higher_is_better,
    )
    for name, dimension, kind, higher_is_better in _SPEC_TABLE
}

# metric registry 的当前版本（`EvalScoringSpec.metric_registry_version` 用）。
METRIC_REGISTRY_VERSION = 1


class MetricValue(BaseModel):
    """一次测量的值对象（variant/case 归属由上层容器承载，这里只存测量本身）。"""

    model_config = ConfigDict(frozen=True)

    metric_name: MetricName
    metric_version: int = Field(default=1, ge=1)
    status: MetricStatus
    value: Decimal | None = None
    numerator: Decimal | None = None
    denominator: Decimal | None = None
    sample_count: int = Field(default=0, ge=0)
    reason_code: str | None = None

    @field_validator("reason_code")
    @classmethod
    def _validate_reason_code(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.strip()
            if not v:
                raise ValueError("reason_code 非空时不能为空白")
        return v

    @model_validator(mode="after")
    def _validate_status_value(self) -> "MetricValue":
        if self.status == MetricStatus.COMPUTED:
            if self.value is None:
                raise ValueError("status=computed 时 value 必须非 None")
        elif self.value is not None:
            raise ValueError("status != computed 时 value 必须为 None")
        if self.denominator is not None and self.denominator == 0:
            raise ValueError("denominator 不能为 0（无 eligible 样本用 not_applicable）")
        return self
