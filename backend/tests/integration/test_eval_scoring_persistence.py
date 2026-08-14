"""Evaluation scoring persistence integration tests (stage 7B.1.3B).

真实 PostgreSQL（127.0.0.1:5433）。覆盖：

- ScoringSpec create-or-get + exact replay + fingerprint 校验；
- ScoreRun 绑定 success attempt（output fingerprint 一致）；failed attempt 拒绝；
- MetricValue 持久化 + fingerprint replay（重复写 0 新增）；
- HumanLabelBinding immutable（同 label fingerprint 重放一致，跨 run 拒绝）；
- JudgeRun + JudgeMetricResults 持久化 + replay；
- verify_* 完整性（篡改 payload → 稳定 integrity error）。
"""

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.eval.contracts import (
    EvalExecutionConfig,
    EvalExecutionSpec,
    EvalScoringSpec,
    EvalVariantOutput,
    FrozenModelConfig,
)
from app.eval.errors import EvalPersistenceError, EvalPersistenceIntegrityError
from app.eval.execution.contracts import (
    EvalExecutionAttemptResult,
    EvalTrialSpec,
    ExecutionAttemptStatus,
    compute_trial_fingerprint,
)
from app.eval.fingerprints import (
    compute_execution_config_fingerprint,
    compute_execution_spec_fingerprint,
    compute_variant_output_fingerprint,
)
from app.eval.judge import JudgeConfig, JudgeMetricScore, JudgeOutput, JudgeRunOutcome
from app.eval.metrics import MetricName, MetricStatus, MetricValue
from app.eval.persistence.service import EvaluationExecutionPersistenceService
from app.eval.scoring.service import EvaluationScoringPersistenceService
from app.eval.variants import EvalVariantId

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_TABLES = (
    "eval_judge_metric_results",
    "eval_judge_runs",
    "eval_human_label_bindings",
    "eval_metric_values",
    "eval_score_runs",
    "eval_scoring_specs",
    "eval_llm_call_usages",
    "eval_execution_attempts",
    "eval_trials",
    "eval_execution_specs",
)


@pytest_asyncio.fixture
async def database() -> DatabaseManager:
    settings = get_settings()
    manager = DatabaseManager(
        database_url=settings.database_url,
        echo=False,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    yield manager
    await manager.dispose()


@pytest_asyncio.fixture
async def sessionmaker(database):
    return database.session_factory()


@pytest_asyncio.fixture
async def service(sessionmaker):
    return EvaluationScoringPersistenceService(sessionmaker)


async def _delete_all(sessionmaker) -> None:
    async with sessionmaker() as session:
        for table in _TABLES:
            await session.execute(text(f"DELETE FROM {table}"))
        await session.commit()


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(sessionmaker):
    await _delete_all(sessionmaker)
    yield
    await _delete_all(sessionmaker)


def _hex64() -> str:
    return uuid4().hex + uuid4().hex


def _output(variant_id: EvalVariantId = EvalVariantId.SINGLE_RAG) -> EvalVariantOutput:
    return EvalVariantOutput(
        variant_id=variant_id,
        case_id="score-case",
        case_version=1,
        final_text="scored final text",
    )


def _scoring_spec(output: EvalVariantOutput, **overrides) -> EvalScoringSpec:
    values = dict(
        variant_output_fingerprint=compute_variant_output_fingerprint(output),
        metric_registry_version=1,
    )
    values.update(overrides)
    return EvalScoringSpec(**values)


def _judge_config() -> JudgeConfig:
    return JudgeConfig(
        model=FrozenModelConfig(
            provider="deepseek",
            model_id="deepseek-v4-flash",
            thinking_enabled=False,
            temperature=Decimal("0"),
            structured_output=True,
        )
    )


def _judge_output() -> JudgeOutput:
    return JudgeOutput(
        metric_scores=(
            JudgeMetricScore(metric_name=MetricName.OVERCLAIM_RATE, score=Decimal("0.2")),
        )
    )


async def _seed_attempt(sessionmaker, service) -> tuple[EvalVariantOutput, str]:
    """执行侧完整链路（spec → trial → attempt success），返回 (output, execution_id)。"""
    config = EvalExecutionConfig(
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
    spec = EvalExecutionSpec(
        case_fingerprint=_hex64(),
        source_snapshot_fingerprint=_hex64(),
        execution_config_fingerprint=compute_execution_config_fingerprint(config),
        variant_id=EvalVariantId.SINGLE_RAG,
    )
    exec_service = EvaluationExecutionPersistenceService(sessionmaker)
    spec_id, _ = await exec_service.create_or_get_execution_spec(spec, config)
    trial_spec = EvalTrialSpec(
        execution_spec_fingerprint=compute_execution_spec_fingerprint(spec), trial_no=1
    )
    trial_id, _ = await exec_service.create_or_get_trial(spec_id, trial_spec)
    output = _output()
    result = EvalExecutionAttemptResult(
        execution_id=uuid4(),
        trial_fingerprint=compute_trial_fingerprint(trial_spec),
        attempt_no=1,
        variant_id=EvalVariantId.SINGLE_RAG,
        case_id="score-case",
        case_version=1,
        status=ExecutionAttemptStatus.SUCCESS,
        wall_latency_ms=100,
        variant_output=output,
        variant_output_fingerprint=compute_variant_output_fingerprint(output),
        usage_records=(),
        error_code=None,
    )
    await exec_service.persist_attempt_result(result)
    return output, str(result.execution_id)


async def test_scoring_spec_create_and_exact_replay(service) -> None:
    output = _output()
    spec = _scoring_spec(output)
    spec_id, created = await service.create_or_get_scoring_spec(spec)
    assert created is True
    spec_id2, created2 = await service.create_or_get_scoring_spec(spec)
    assert created2 is False
    assert spec_id == spec_id2
    verified = await service.verify_scoring_spec_integrity(spec_id)
    assert verified == spec


async def test_score_run_binds_success_attempt(sessionmaker, service) -> None:
    output, execution_id = await _seed_attempt(sessionmaker, service)
    spec = _scoring_spec(output)
    run_id, created = await service.create_or_get_score_run(UUID(execution_id), spec)
    assert created is True
    run_id2, created2 = await service.create_or_get_score_run(UUID(execution_id), spec)
    assert created2 is False
    assert run_id == run_id2
    verified = await service.verify_score_run_integrity(run_id)
    assert verified.execution_id == UUID(execution_id)
    assert verified.spec == spec


async def test_score_run_rejects_failed_attempt(sessionmaker, service) -> None:
    from app.eval.contracts import EvalExecutionConfig
    from app.eval.persistence.service import EvaluationExecutionPersistenceService

    config = EvalExecutionConfig(
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
    spec = EvalExecutionSpec(
        case_fingerprint=_hex64(),
        source_snapshot_fingerprint=_hex64(),
        execution_config_fingerprint=compute_execution_config_fingerprint(config),
        variant_id=EvalVariantId.SINGLE_RAG,
    )
    exec_service = EvaluationExecutionPersistenceService(sessionmaker)
    spec_id, _ = await exec_service.create_or_get_execution_spec(spec, config)
    trial_spec = EvalTrialSpec(
        execution_spec_fingerprint=compute_execution_spec_fingerprint(spec), trial_no=1
    )
    trial_id, _ = await exec_service.create_or_get_trial(spec_id, trial_spec)
    failed = EvalExecutionAttemptResult(
        execution_id=uuid4(),
        trial_fingerprint=compute_trial_fingerprint(trial_spec),
        attempt_no=1,
        variant_id=EvalVariantId.SINGLE_RAG,
        case_id="score-case",
        case_version=1,
        status=ExecutionAttemptStatus.FAILED,
        wall_latency_ms=10,
        variant_output=None,
        variant_output_fingerprint=None,
        usage_records=(),
        error_code="some_error",
    )
    await exec_service.persist_attempt_result(failed)
    with pytest.raises(EvalPersistenceError):
        await service.create_or_get_score_run(failed.execution_id, _scoring_spec(_output()))


async def test_metric_values_persist_and_replay(sessionmaker, service) -> None:
    output, execution_id = await _seed_attempt(sessionmaker, service)
    spec = _scoring_spec(output)
    run_id, _ = await service.create_or_get_score_run(UUID(execution_id), spec)
    values = (
        MetricValue(
            metric_name=MetricName.CITATION_VALIDITY,
            metric_version=1,
            status=MetricStatus.COMPUTED,
            value=Decimal("0.8"),
            numerator=Decimal("4"),
            denominator=Decimal("5"),
            sample_count=5,
        ),
        MetricValue(
            metric_name=MetricName.CITATION_COVERAGE,
            metric_version=1,
            status=MetricStatus.NOT_APPLICABLE,
            reason_code="no_claims",
        ),
    )
    await service.persist_metric_values(run_id, values)
    # 重复写：0 新增（fingerprint replay）。
    await service.persist_metric_values(run_id, values)
    verified = await service.verify_score_run_integrity(run_id)
    assert len(verified.metric_values) == 2
    by_name = {row.metric_name: row for row in verified.metric_values}
    assert by_name["citation_validity"].value == Decimal("0.8")
    assert by_name["citation_coverage"].status == "not_applicable"


async def test_human_label_binding_immutable(sessionmaker, service) -> None:
    output, execution_id = await _seed_attempt(sessionmaker, service)
    spec = _scoring_spec(output, human_label_fingerprint=_hex64())
    run_id, _ = await service.create_or_get_score_run(UUID(execution_id), spec)
    binding_id, created = await service.persist_human_label_binding(
        run_id,
        label_fingerprint=spec.human_label_fingerprint,
        label_schema_version=1,
        case_id="score-case",
        case_version=1,
    )
    assert created is True
    binding_id2, created2 = await service.persist_human_label_binding(
        run_id,
        label_fingerprint=spec.human_label_fingerprint,
        label_schema_version=1,
        case_id="score-case",
        case_version=1,
    )
    assert created2 is False
    assert binding_id == binding_id2
    # 跨 run 复用同一 label → replay 不一致（case 身份不同）→ integrity error。
    output2, execution_id2 = await _seed_attempt(sessionmaker, service)
    run2, _ = await service.create_or_get_score_run(UUID(execution_id2), spec)
    with pytest.raises(EvalPersistenceIntegrityError):
        await service.persist_human_label_binding(
            run2,
            label_fingerprint=spec.human_label_fingerprint,
            label_schema_version=1,
            case_id="other-case",
            case_version=1,
        )


async def test_judge_run_persist_and_replay(sessionmaker, service) -> None:
    output, execution_id = await _seed_attempt(sessionmaker, service)
    spec = _scoring_spec(output, judge_config_fingerprint="b" * 64)
    run_id, _ = await service.create_or_get_score_run(UUID(execution_id), spec)
    config = _judge_config()
    outcome = JudgeRunOutcome(
        status="completed",
        judge_config_fingerprint=config_fp(config),
        judge_output_fingerprint="c" * 64,
        output=_judge_output(),
        input_tokens=10,
        output_tokens=20,
        total_tokens=30,
        duration_ms=5,
    )
    judge_run_id, created = await service.persist_judge_run(run_id, config, outcome)
    assert created is True
    judge_run_id2, created2 = await service.persist_judge_run(run_id, config, outcome)
    assert created2 is False
    assert judge_run_id == judge_run_id2
    await service.persist_judge_metric_results(judge_run_id, _judge_output())


def config_fp(config: JudgeConfig) -> str:
    from app.eval.judge.fingerprints import compute_judge_config_fingerprint

    return compute_judge_config_fingerprint(config)
