"""LLM judge service (stage 7B.1.3C).

`JudgeService` 对 `JudgeInput` 运行 versioned judge（结构化输出 + usage 遥测 +
失败重试）。底层 chat model 由调用方构造注入（生产 `ChatDeepSeek`；测试注入
fake），judge 不 import 任何生产 adapter。

- component_name = `eval_judge`（eval-only，**不**在 production 10-component
  registry）；
- 失败（provider / malformed）→ 稳定 `JudgeRunOutcome(status=failed)`（可重试，
  **不**伪装 deterministic truth）；success → `JudgeOutput` 指纹 + usage 汇总。
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import ValidationError

from app.eval.errors import EvalJudgeError
from app.eval.judge.contracts import (
    JudgeConfig,
    JudgeInput,
    JudgeOutput,
    JudgeRunOutcome,
)
from app.eval.judge.fingerprints import (
    compute_judge_config_fingerprint,
    compute_judge_output_fingerprint,
)
from app.eval.judge.prompts import build_judge_messages
from app.llm.components import COMPONENT_EVAL_JUDGE
from app.llm.instrumentation import LlmUsageObserver, invoke_structured_with_usage

# 失败重试次数（v1：1 次重试；provider/parsing 失败可重试，确定性校验失败不重试）。
JUDGE_MAX_RETRIES = 1


class JudgeService:
    """versioned semantic judge：JudgeInput → JudgeOutput（+ usage + retry）。"""

    def __init__(
        self,
        model: Any,
        config: JudgeConfig,
        usage_observer: LlmUsageObserver | None = None,
    ) -> None:
        self._model = model
        self._config = config
        self._observer = usage_observer
        self._config_fingerprint = compute_judge_config_fingerprint(config)

    @property
    def config_fingerprint(self) -> str:
        return self._config_fingerprint

    @property
    def config(self) -> JudgeConfig:
        return self._config

    async def run_judge(self, judge_input: JudgeInput) -> JudgeRunOutcome:
        """执行一次 judge（重试语义见模块 docstring）。"""
        messages = build_judge_messages(judge_input)
        last_error: BaseException | None = None
        for _attempt in range(JUDGE_MAX_RETRIES + 1):
            start = time.monotonic()
            try:
                raw = await invoke_structured_with_usage(
                    self._model,
                    JudgeOutput,
                    messages,
                    component_name=COMPONENT_EVAL_JUDGE,
                    provider=self._config.model.provider,
                    model_id=self._config.model.model_id,
                    usage_observer=self._observer,
                )
            except Exception as exc:  # noqa: BLE001 — provider/parsing 失败统一重试
                last_error = exc
                continue
            duration_ms = max(0, int(round((time.monotonic() - start) * 1000)))
            try:
                output = raw if isinstance(raw, JudgeOutput) else JudgeOutput.model_validate(raw)
            except ValidationError as exc:
                last_error = exc
                continue
            return JudgeRunOutcome(
                status="completed",
                judge_config_fingerprint=self._config_fingerprint,
                judge_output_fingerprint=compute_judge_output_fingerprint(output),
                output=output,
                duration_ms=duration_ms,
            )
        # 全部重试失败 → 稳定 failed outcome（不泄漏 raw error 文本）。
        code = getattr(last_error, "code", None)
        return JudgeRunOutcome(
            status="failed",
            judge_config_fingerprint=self._config_fingerprint,
            error_code=code if isinstance(code, str) and code.strip() else "eval_judge_error",
            duration_ms=None,
        )

    async def require_judge(self, judge_input: JudgeInput) -> tuple[JudgeOutput, str]:
        """success-or-raise：judge 失败 → `EvalJudgeError`（稳定 code）。"""
        outcome = await self.run_judge(judge_input)
        if outcome.status != "completed" or outcome.output is None:
            raise EvalJudgeError(outcome.error_code or "eval_judge_error")
        return outcome.output, outcome.judge_output_fingerprint
