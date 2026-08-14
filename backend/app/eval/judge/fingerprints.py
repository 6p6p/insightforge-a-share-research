"""Judge fingerprints (stage 7B.1.3C).

judge config fingerprint（`compute_judge_config_fingerprint`）标识 judge 身份
（name / version / prompt_version / model / temperature / max_output_tokens）；
judge output fingerprint（`compute_judge_output_fingerprint`）标识一次结构化
输出（全部 metric scores，canonical 排序）。两者都属于 **scoring layer**，
**不**进入 variant 的 `execution_config_fingerprint`。
"""

import hashlib
from decimal import Decimal

from app.eval.canonical import canonical_json_str
from app.eval.judge.contracts import JudgeConfig, JudgeOutput


def _decimal(value: Decimal) -> str:
    return str(value)


def compute_judge_config_fingerprint(config: JudgeConfig) -> str:
    payload = {
        "schema_version": config.schema_version,
        "judge_name": config.judge_name,
        "judge_version": config.judge_version,
        "prompt_version": config.prompt_version,
        "model": {
            "provider": config.model.provider,
            "model_id": config.model.model_id,
            "thinking_enabled": config.model.thinking_enabled,
            "temperature": _decimal(config.model.temperature),
            "max_output_tokens": config.model.max_output_tokens,
            "structured_output": config.model.structured_output,
        },
        "temperature": _decimal(config.temperature),
        "max_output_tokens": config.max_output_tokens,
    }
    return hashlib.sha256(canonical_json_str(payload).encode("utf-8")).hexdigest()


def compute_judge_output_fingerprint(output: JudgeOutput) -> str:
    scores = sorted(
        [
            {
                "metric_name": item.metric_name.value,
                "score": _decimal(item.score),
                "rationale_ref": item.rationale_ref,
            }
            for item in output.metric_scores
        ],
        key=lambda item: item["metric_name"],
    )
    payload = {"schema_version": 1, "metric_scores": scores}
    return hashlib.sha256(canonical_json_str(payload).encode("utf-8")).hexdigest()
