"""LLM judge contracts (stage 7B.1.3C).

`insightforge_full` 之外的独立 judge：对 normalized `EvalVariantOutput` 做
semantic 评分（overclaim / claim_support 等无法 purely deterministic 的指标）。

身份与隔离：
- `JudgeConfig` 是 versioned judge 身份（judge_name / judge_version /
  prompt_version / model / temperature / max_output_tokens）——judge config
  fingerprint 属于 **scoring layer**，**不**进入 variant 的
  `execution_config_fingerprint`；
- `JudgeInput` 只含 variant 实际看到的信息（variant output + source snapshot
  fingerprint + research question + analysis_as_of + case 语义身份），**不含**
  HumanLabel / 其它 variant 的输出 / runtime 身份；
- `JudgeOutput` 是结构化逐指标评分（score ∈ [0,1] + 短 rationale_ref），
  rationale 不保存长 prose；judge 失败（provider / malformed）→ 稳定
  `EvalJudgeError`（可重试，不伪装 deterministic truth）。
"""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.eval.contracts import FrozenModelConfig, _strip, _validate_sha256
from app.eval.metrics import MetricName

JUDGE_SCHEMA_VERSION = 1

# judge 身份常量（v1）。
JUDGE_NAME = "insightforge_semantic_judge"
JUDGE_VERSION = 1

# prompt 版本（进入 judge config fingerprint；prompt 变更 → 新 fingerprint → 新
# judge 身份，旧结果保留）。
JUDGE_PROMPT_VERSION = "v1"

# 一次 judge 输出的指标上限（防 prompt 膨胀）。
MAX_JUDGE_METRICS = 8


class JudgeConfig(BaseModel):
    """versioned judge 执行配置（scoring layer；不进 variant execution config）。"""

    model_config = ConfigDict(frozen=True)

    schema_version: int = JUDGE_SCHEMA_VERSION
    judge_name: str = JUDGE_NAME
    judge_version: int = JUDGE_VERSION
    prompt_version: str = JUDGE_PROMPT_VERSION
    model: FrozenModelConfig
    temperature: Decimal = Field(default=Decimal("0"), ge=0, le=2)
    max_output_tokens: int | None = Field(default=None, ge=1)

    @field_validator("judge_name", "prompt_version")
    @classmethod
    def _v_nonempty(cls, v: str) -> str:
        return _strip(v, field="judge config 字段")

    @field_validator("judge_version")
    @classmethod
    def _v_version(cls, v: int) -> int:
        if v < 1:
            raise ValueError("judge_version 必须 >= 1")
        return v


class JudgeInput(BaseModel):
    """judge 的输入投影（只含 variant 实际看到的信息，无 label / 其它 variant）。"""

    model_config = ConfigDict(frozen=True)

    case_id: str
    case_version: int = Field(ge=1)
    variant_id: str
    research_question: str
    analysis_as_of: str
    source_snapshot_fingerprint: str
    final_text: str
    claims: tuple[dict, ...] = ()
    citations: tuple[dict, ...] = ()

    @field_validator("case_id")
    @classmethod
    def _v_case_id(cls, v: str) -> str:
        return _strip(v, field="case_id")

    @field_validator("research_question")
    @classmethod
    def _v_question(cls, v: str) -> str:
        return _strip(v, field="research_question", max_len=4000)

    @field_validator("source_snapshot_fingerprint")
    @classmethod
    def _v_sha(cls, v: str) -> str:
        return _validate_sha256(v, field="source_snapshot_fingerprint")


class JudgeMetricScore(BaseModel):
    """judge 对单个 metric 的评分。"""

    model_config = ConfigDict(frozen=True)

    metric_name: MetricName
    score: Decimal = Field(ge=0, le=1)
    rationale_ref: str | None = None

    @field_validator("rationale_ref")
    @classmethod
    def _v_rationale(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.strip()
            # 只允许短引用（防长 prose 进入持久化）。
            if len(v) > 200:
                raise ValueError("rationale_ref 最长 200 字符")
            if not v:
                return None
        return v


class JudgeOutput(BaseModel):
    """judge 的结构化输出（逐指标评分）。"""

    model_config = ConfigDict(frozen=True)

    metric_scores: tuple[JudgeMetricScore, ...] = Field(min_length=1, max_length=MAX_JUDGE_METRICS)

    @model_validator(mode="after")
    def _reject_duplicates(self) -> "JudgeOutput":
        names = [item.metric_name for item in self.metric_scores]
        if len(names) != len(set(names)):
            raise ValueError("metric_scores 不允许重复 metric_name")
        return self


class JudgeRunOutcome(BaseModel):
    """一次 judge 执行的结果（success 或稳定失败）。"""

    model_config = ConfigDict(frozen=True)

    judge_run_id: str | None = None
    status: Literal["completed", "failed"]
    judge_config_fingerprint: str
    judge_output_fingerprint: str | None = None
    output: JudgeOutput | None = None
    error_code: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    duration_ms: int | None = None

    @field_validator("judge_config_fingerprint", "judge_output_fingerprint")
    @classmethod
    def _v_sha(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _validate_sha256(v, field="judge fingerprint")
