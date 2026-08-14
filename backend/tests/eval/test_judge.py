"""Judge service unit tests (stage 7B.1.3C).

0 DB / 0 network / 0 真实 DeepSeek：fake chat model（结构化输出 / parsing 失败 /
invoke 失败 / 先失败后成功），验证：

- success：JudgeOutput 指纹 + usage 经 observer 记录（component=eval_judge）；
- parsing 失败重试 → 最终 success（先 malformed 后成功）；
- invoke 失败重试耗尽 → 稳定 failed outcome（不抛、不伪装）；
- config fingerprint 稳定且 versioned（prompt 版本变化 → fingerprint 变化）；
- judge input 不含 label / 其它 variant 输出（构造侧断言）。
"""

from decimal import Decimal

import pytest

from app.eval.contracts import FrozenModelConfig
from app.eval.judge import (
    JudgeConfig,
    JudgeInput,
    JudgeMetricScore,
    JudgeOutput,
    JudgeRunOutcome,
    JudgeService,
    compute_judge_config_fingerprint,
    compute_judge_output_fingerprint,
)
from app.eval.metrics import MetricName
from app.llm.components import COMPONENT_EVAL_JUDGE
from app.llm.instrumentation import (
    LlmCallOutcome,
    LlmCallUsageRecord,
    UsageStatus,
)


class _FakeJudgeModel:
    """可控 fake chat model：按调用次序返回 parsed / malformed / 抛错。"""

    def __init__(self, *outcomes) -> None:
        # outcomes: ("ok", JudgeOutput) | ("malformed", dict) | ("raise", Exception)
        self._outcomes = list(outcomes)
        self.calls = 0

    def with_structured_output(self, schema, include_raw: bool = False):
        return _StructuredCallable(self, schema)


class _StructuredCallable:
    def __init__(self, owner, schema) -> None:
        self._owner = owner
        self._schema = schema

    async def ainvoke(self, messages):
        self._owner.calls += 1
        kind, value = self._owner._outcomes[
            min(self._owner.calls - 1, len(self._owner._outcomes) - 1)
        ]
        if kind == "raise":
            raise value
        if kind == "malformed":
            return {
                "parsed": None,
                "raw": type("Raw", (), {"usage_metadata": None})(),
                "parsing_error": ValueError("malformed"),
            }
        parsed = value if isinstance(value, self._schema) else self._schema.model_validate(value)
        return {
            "parsed": parsed,
            "raw": type(
                "Raw",
                (),
                {
                    "usage_metadata": {
                        "input_tokens": 10,
                        "output_tokens": 20,
                        "total_tokens": 30,
                    }
                },
            )(),
            "parsing_error": None,
        }


class _Collector:
    def __init__(self) -> None:
        self.records: list[LlmCallUsageRecord] = []

    async def record(self, record: LlmCallUsageRecord) -> None:
        self.records.append(record)


def _config(**overrides) -> JudgeConfig:
    values = dict(
        model=FrozenModelConfig(
            provider="deepseek",
            model_id="deepseek-v4-flash",
            thinking_enabled=False,
            temperature=Decimal("0"),
            structured_output=True,
        )
    )
    values.update(overrides)
    return JudgeConfig(**values)


def _input() -> JudgeInput:
    return JudgeInput(
        case_id="judge-case",
        case_version=1,
        variant_id="insightforge_full",
        research_question="测试问题",
        analysis_as_of="2026-08-01",
        source_snapshot_fingerprint="a" * 64,
        final_text="测试正文",
    )


def _output() -> JudgeOutput:
    return JudgeOutput(
        metric_scores=(
            JudgeMetricScore(metric_name=MetricName.OVERCLAIM_RATE, score=Decimal("0.1")),
        )
    )


async def _run(model, config=None, observer=None) -> tuple[JudgeService, JudgeRunOutcome]:
    service = JudgeService(model, config or _config(), usage_observer=observer)
    return service, await service.run_judge(_input())


@pytest.mark.asyncio
async def test_judge_success_records_usage_and_fingerprint() -> None:
    collector = _Collector()
    model = _FakeJudgeModel(("ok", _output()))
    service, outcome = await _run(model, observer=collector)

    assert outcome.status == "completed"
    assert outcome.output is not None
    assert outcome.judge_output_fingerprint == compute_judge_output_fingerprint(_output())
    assert outcome.judge_config_fingerprint == service.config_fingerprint
    assert model.calls == 1
    # usage：eval_judge component 一条 reported 记录。
    assert len(collector.records) == 1
    record = collector.records[0]
    assert record.component_name == COMPONENT_EVAL_JUDGE
    assert record.outcome == LlmCallOutcome.SUCCESS
    assert record.usage_status == UsageStatus.REPORTED
    assert record.total_tokens == 30


@pytest.mark.asyncio
async def test_judge_retries_after_parsing_error() -> None:
    model = _FakeJudgeModel(("malformed", {}), ("ok", _output()))
    _, outcome = await _run(model)
    assert outcome.status == "completed"
    assert model.calls == 2


@pytest.mark.asyncio
async def test_judge_retries_exhausted_returns_failed_outcome() -> None:
    model = _FakeJudgeModel(("raise", RuntimeError("boom")), ("raise", RuntimeError("boom")))
    _, outcome = await _run(model)
    assert outcome.status == "failed"
    assert outcome.error_code == "eval_judge_error"
    assert outcome.output is None
    assert model.calls == 2


@pytest.mark.asyncio
async def test_judge_require_raises_stable_error_on_failure() -> None:
    from app.eval.errors import EvalJudgeError

    model = _FakeJudgeModel(("raise", RuntimeError("boom")), ("raise", RuntimeError("boom")))
    service = JudgeService(model, _config())
    with pytest.raises(EvalJudgeError) as exc:
        await service.require_judge(_input())
    assert exc.value.code == "eval_judge_error"


def test_judge_config_fingerprint_versioned() -> None:
    fp1 = compute_judge_config_fingerprint(_config())
    fp2 = compute_judge_config_fingerprint(_config(prompt_version="v2"))
    assert fp1 != fp2
    assert len(fp1) == 64
    # judge config 不影响 variant execution config（构造侧隔离）。
    from app.eval.contracts import EvalExecutionConfig, EvalVariantId
    from app.eval.fingerprints import compute_execution_config_fingerprint

    variant_config = EvalExecutionConfig(
        variant_id=EvalVariantId.SINGLE_RAG,
        model=FrozenModelConfig(
            provider="deepseek",
            model_id="deepseek-v4-flash",
            thinking_enabled=False,
            temperature=Decimal("0"),
            structured_output=True,
        ),
        variant_version="v1",
        prompt_version="v1",
        retrieval_version="v1",
        pipeline_version="v1",
    )
    assert compute_execution_config_fingerprint(variant_config) != fp1


def test_judge_output_fingerprint_canonical() -> None:
    output_a = JudgeOutput(
        metric_scores=(
            JudgeMetricScore(metric_name=MetricName.OVERCLAIM_RATE, score=Decimal("0.1")),
            JudgeMetricScore(metric_name=MetricName.CLAIM_SUPPORT_RATE, score=Decimal("0.9")),
        )
    )
    output_b = JudgeOutput(
        metric_scores=(
            JudgeMetricScore(metric_name=MetricName.CLAIM_SUPPORT_RATE, score=Decimal("0.9")),
            JudgeMetricScore(metric_name=MetricName.OVERCLAIM_RATE, score=Decimal("0.1")),
        )
    )
    assert compute_judge_output_fingerprint(output_a) == compute_judge_output_fingerprint(output_b)
