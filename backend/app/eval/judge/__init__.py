"""Evaluation LLM judge (stage 7B.1.3C).

独立 semantic judge：对 normalized `EvalVariantOutput` 做结构化评分
（overclaim / claim_support 等无法 purely deterministic 的指标）。judge
config / prompt / output 全部 versioned；judge 身份（config fingerprint）属于
**scoring layer**，不进入 variant 的 `execution_config_fingerprint`。
"""

from app.eval.judge.contracts import (
    JUDGE_NAME,
    JUDGE_PROMPT_VERSION,
    JUDGE_SCHEMA_VERSION,
    JUDGE_VERSION,
    MAX_JUDGE_METRICS,
    JudgeConfig,
    JudgeInput,
    JudgeMetricScore,
    JudgeOutput,
    JudgeRunOutcome,
)
from app.eval.judge.fingerprints import (
    compute_judge_config_fingerprint,
    compute_judge_output_fingerprint,
)
from app.eval.judge.service import JudgeService

__all__ = [
    "JUDGE_NAME",
    "JUDGE_PROMPT_VERSION",
    "JUDGE_SCHEMA_VERSION",
    "JUDGE_VERSION",
    "MAX_JUDGE_METRICS",
    "JudgeConfig",
    "JudgeInput",
    "JudgeMetricScore",
    "JudgeOutput",
    "JudgeRunOutcome",
    "JudgeService",
    "compute_judge_config_fingerprint",
    "compute_judge_output_fingerprint",
]
