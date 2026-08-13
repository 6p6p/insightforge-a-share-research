"""EvaluationExecutionPersistenceService integration tests (stage 7B.1.3A, spec T).

在真实 PostgreSQL（127.0.0.1:5433）验证 `ExecutionSpec 1:N Trial 1:N Attempt
1:N LLM Call Usage` 四层持久化（spec H/O/P/Q/R）：

- create-or-get 的 replay 语义（exact replay 返回同 id，不一致 = 完整性错误）；
- trial / attempt 的 parent 一致性 + fingerprint 重校验；
- success/failed 与 output fp/payload、error_code 的互斥（DB CHECK 兜底）；
- usage 的 token 完整性（DB CHECK）与 call_index 连续（verifier）；
- attempt replay / 并发同 attempt 的去重；
- verifier 对 payload / output / usage 篡改的拒绝。

零 LLM / 零 network。全部 synthetic frozen contract，不触碰其余业务表。
"""

import asyncio
import json
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.eval_execution import (
    EvalExecutionAttemptModel,
    EvalLlmCallUsageModel,
)
from app.db.session import DatabaseManager
from app.eval.contracts import (
    EvalExecutionConfig,
    EvalExecutionSpec,
    EvalVariantOutput,
    FrozenModelConfig,
)
from app.eval.errors import EvalPersistenceIntegrityError
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
from app.eval.persistence import EvaluationExecutionPersistenceService
from app.eval.variants import EvalVariantId
from app.llm.instrumentation import LlmCallOutcome, LlmCallUsageRecord, UsageStatus

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_EVAL_TABLES = (
    "eval_llm_call_usages",
    "eval_execution_attempts",
    "eval_trials",
    "eval_execution_specs",
)


# --------------------------------------------------------------------- fixtures


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
    return EvaluationExecutionPersistenceService(sessionmaker)


async def _delete_all(sessionmaker) -> None:
    async with sessionmaker() as session:
        for table in _EVAL_TABLES:
            await session.execute(text(f"DELETE FROM {table}"))
        await session.commit()


@pytest_asyncio.fixture(autouse=True)
async def _clean_eval_tables(sessionmaker):
    await _delete_all(sessionmaker)
    yield
    await _delete_all(sessionmaker)


# --------------------------------------------------------------------- builders


def _hex64() -> str:
    return uuid4().hex + uuid4().hex


def _config(variant_id: EvalVariantId = EvalVariantId.INSIGHTFORGE_FULL) -> EvalExecutionConfig:
    return EvalExecutionConfig(
        variant_id=variant_id,
        model=FrozenModelConfig(
            provider="deepseek",
            model_id="deepseek-v4-flash",
            thinking_enabled=False,
            temperature=Decimal("0"),
            structured_output=True,
        ),
        variant_version="1",
        prompt_version="1",
        retrieval_version="1",
        pipeline_version="1",
    )


def _spec(config: EvalExecutionConfig) -> EvalExecutionSpec:
    return EvalExecutionSpec(
        case_fingerprint=_hex64(),
        source_snapshot_fingerprint=_hex64(),
        execution_config_fingerprint=compute_execution_config_fingerprint(config),
        variant_id=config.variant_id,
    )


def _trial(spec: EvalExecutionSpec, trial_no: int = 1) -> EvalTrialSpec:
    return EvalTrialSpec(
        execution_spec_fingerprint=compute_execution_spec_fingerprint(spec),
        trial_no=trial_no,
    )


def _output(variant_id: EvalVariantId, case_id: str = "test-case") -> EvalVariantOutput:
    return EvalVariantOutput(
        variant_id=variant_id,
        case_id=case_id,
        case_version=1,
        final_text="test final text",
    )


def _usage_reported(
    *, component: str = "audit", in_tok: int = 100, out_tok: int = 50
) -> LlmCallUsageRecord:
    return LlmCallUsageRecord(
        component_name=component,
        provider="deepseek",
        model_id="deepseek-v4-flash",
        outcome=LlmCallOutcome.SUCCESS,
        duration_ms=10,
        usage_status=UsageStatus.REPORTED,
        input_tokens=in_tok,
        output_tokens=out_tok,
        total_tokens=in_tok + out_tok,
    )


def _usage_unavailable() -> LlmCallUsageRecord:
    return LlmCallUsageRecord(
        component_name="audit",
        provider="deepseek",
        model_id="deepseek-v4-flash",
        outcome=LlmCallOutcome.INVOCATION_ERROR,
        duration_ms=10,
        usage_status=UsageStatus.UNAVAILABLE,
    )


def _success_result(
    trial: EvalTrialSpec,
    output: EvalVariantOutput,
    *,
    execution_id=None,
    attempt_no: int = 1,
    usage=(),
) -> EvalExecutionAttemptResult:
    return EvalExecutionAttemptResult(
        execution_id=execution_id or uuid4(),
        trial_fingerprint=compute_trial_fingerprint(trial),
        attempt_no=attempt_no,
        variant_id=output.variant_id,
        case_id=output.case_id,
        case_version=output.case_version,
        status=ExecutionAttemptStatus.SUCCESS,
        wall_latency_ms=5,
        variant_output=output,
        variant_output_fingerprint=compute_variant_output_fingerprint(output),
        usage_records=tuple(usage),
        error_code=None,
    )


def _failed_result(
    trial: EvalTrialSpec,
    variant_id: EvalVariantId,
    *,
    attempt_no: int = 1,
    execution_id=None,
) -> EvalExecutionAttemptResult:
    return EvalExecutionAttemptResult(
        execution_id=execution_id or uuid4(),
        trial_fingerprint=compute_trial_fingerprint(trial),
        attempt_no=attempt_no,
        variant_id=variant_id,
        case_id="test-case",
        case_version=1,
        status=ExecutionAttemptStatus.FAILED,
        wall_latency_ms=5,
        variant_output=None,
        variant_output_fingerprint=None,
        usage_records=(),
        error_code="EVAL_RUNNER_ERROR",
    )


async def _persist_spec_trial(service):
    """建 spec + trial 并返回 (spec, config, spec_id, trial, trial_id)。"""
    config = _config()
    spec = _spec(config)
    spec_id, _ = await service.create_or_get_execution_spec(spec, config)
    trial = _trial(spec, trial_no=1)
    trial_id, _ = await service.create_or_get_trial(spec_id, trial)
    return spec, config, spec_id, trial, trial_id


# --------------------------------------------------------------------- spec


async def test_create_execution_spec_and_verify(service) -> None:
    config = _config()
    spec = _spec(config)
    spec_id, created = await service.create_or_get_execution_spec(spec, config)
    assert created is True

    verified = await service.verify_execution_spec_integrity(spec_id)
    assert verified.execution_spec_id == spec_id
    assert verified.spec.case_fingerprint == spec.case_fingerprint
    assert verified.spec.source_snapshot_fingerprint == spec.source_snapshot_fingerprint
    assert verified.config.variant_id == config.variant_id
    assert verified.config.model.model_id == config.model.model_id


async def test_create_execution_spec_exact_replay(service) -> None:
    config = _config()
    spec = _spec(config)
    spec_id, created = await service.create_or_get_execution_spec(spec, config)
    assert created is True

    replay_id, created_again = await service.create_or_get_execution_spec(spec, config)
    assert created_again is False
    assert replay_id == spec_id


async def test_create_execution_spec_config_fingerprint_mismatch_rejected(service) -> None:
    config = _config()
    spec = _spec(config)
    bad_spec = EvalExecutionSpec(
        case_fingerprint=spec.case_fingerprint,
        source_snapshot_fingerprint=spec.source_snapshot_fingerprint,
        execution_config_fingerprint=_hex64(),  # 与 config 不一致
        variant_id=spec.variant_id,
    )
    with pytest.raises(EvalPersistenceIntegrityError, match="config fingerprint"):
        await service.create_or_get_execution_spec(bad_spec, config)


async def test_create_execution_spec_config_variant_mismatch_rejected(service) -> None:
    config = _config(EvalVariantId.SINGLE_RAG)
    spec = EvalExecutionSpec(
        case_fingerprint=_hex64(),
        source_snapshot_fingerprint=_hex64(),
        execution_config_fingerprint=compute_execution_config_fingerprint(config),
        variant_id=EvalVariantId.INSIGHTFORGE_FULL,  # 与 config.variant_id 不一致
    )
    with pytest.raises(EvalPersistenceIntegrityError, match="variant_id"):
        await service.create_or_get_execution_spec(spec, config)


# --------------------------------------------------------------------- trial


async def test_trial1_trial2_coexist(service) -> None:
    spec, _config_, spec_id, _trial1, _trial_id1 = await _persist_spec_trial(service)
    trial2 = _trial(spec, trial_no=2)
    trial_id2, created2 = await service.create_or_get_trial(spec_id, trial2)
    assert created2 is True

    v1 = await service.verify_trial_integrity(_trial_id1)
    v2 = await service.verify_trial_integrity(trial_id2)
    assert v1.trial_spec.trial_no == 1
    assert v2.trial_spec.trial_no == 2
    assert v1.execution_spec_id == spec_id == v2.execution_spec_id


async def test_create_trial_parent_fingerprint_mismatch_rejected(service) -> None:
    spec, _config_, spec_id, _trial1, _trial_id1 = await _persist_spec_trial(service)
    bad_trial = EvalTrialSpec(execution_spec_fingerprint=_hex64(), trial_no=1)
    with pytest.raises(EvalPersistenceIntegrityError, match="父 spec"):
        await service.create_or_get_trial(spec_id, bad_trial)


# --------------------------------------------------------------------- attempt


async def test_attempt_failed_then_success_coexist(service) -> None:
    spec, _config_, _spec_id, trial, _trial_id = await _persist_spec_trial(service)
    failed = _failed_result(trial, spec.variant_id, attempt_no=1)
    await service.persist_attempt_result(failed)

    output = _output(spec.variant_id)
    success = _success_result(trial, output, attempt_no=2)
    await service.persist_attempt_result(success)

    vf = await service.verify_attempt_integrity(failed.execution_id)
    vs = await service.verify_attempt_integrity(success.execution_id)
    assert vf.status == ExecutionAttemptStatus.FAILED
    assert vf.error_code == "EVAL_RUNNER_ERROR"
    assert vs.status == ExecutionAttemptStatus.SUCCESS
    assert vs.variant_output is not None


async def test_attempt_success_output_fingerprint_verified(service) -> None:
    spec, _config_, _spec_id, trial, _trial_id = await _persist_spec_trial(service)
    output = _output(spec.variant_id)
    result = _success_result(trial, output)
    await service.persist_attempt_result(result)

    verified = await service.verify_attempt_integrity(result.execution_id)
    assert verified.variant_output_fingerprint == compute_variant_output_fingerprint(output)
    assert verified.variant_output.final_text == "test final text"


async def test_attempt_failed_db_check_rejects_output(sessionmaker, service) -> None:
    spec, _config_, _spec_id, _trial, trial_id = await _persist_spec_trial(service)
    with pytest.raises(IntegrityError):
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO eval_execution_attempts "
                    "(execution_id, trial_id, attempt_no, status, wall_latency_ms, "
                    " variant_output_fingerprint, variant_output_payload, error_code) "
                    "VALUES (CAST(:eid AS uuid), CAST(:tid AS uuid), 1, 'failed', 5, "
                    " :fp, CAST(:payload AS jsonb), 'x')"
                ).bindparams(
                    eid=str(uuid4()),
                    tid=str(trial_id),
                    fp="a" * 64,
                    payload=json.dumps({"final_text": "x"}),
                )
            )
            await session.commit()


# --------------------------------------------------------------------- usage


async def test_usage_roundtrip(service) -> None:
    spec, _config_, _spec_id, trial, _trial_id = await _persist_spec_trial(service)
    output = _output(spec.variant_id)
    u1 = _usage_reported(component="evidence", in_tok=10, out_tok=5)
    u2 = _usage_unavailable()
    result = _success_result(trial, output, usage=(u1, u2))
    await service.persist_attempt_result(result)

    verified = await service.verify_attempt_integrity(result.execution_id)
    assert len(verified.usage_records) == 2
    assert verified.usage_records[0].component_name == "evidence"
    assert verified.usage_records[0].input_tokens == 10
    assert verified.usage_records[0].output_tokens == 5
    assert verified.usage_records[0].total_tokens == 15
    assert verified.usage_records[0].usage_status == UsageStatus.REPORTED
    assert verified.usage_records[1].usage_status == UsageStatus.UNAVAILABLE
    assert verified.usage_records[1].input_tokens is None


async def test_usage_reported_token_sum_db_check(sessionmaker, service) -> None:
    spec, _config_, _spec_id, trial, _trial_id = await _persist_spec_trial(service)
    output = _output(spec.variant_id)
    result = _success_result(trial, output)
    await service.persist_attempt_result(result)

    with pytest.raises(IntegrityError):
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO eval_llm_call_usages "
                    "(usage_id, execution_id, call_index, component_name, provider, model_id, "
                    " outcome, duration_ms, usage_status, input_tokens, output_tokens, "
                    " total_tokens) "
                    "VALUES (CAST(:uid AS uuid), CAST(:eid AS uuid), 0, 'audit', 'deepseek', 'm', "
                    " 'success', 10, 'reported', 100, 50, 999)"
                ).bindparams(uid=str(uuid4()), eid=str(result.execution_id))
            )
            await session.commit()


async def test_usage_token_tamper_db_check_rejects(sessionmaker, service) -> None:
    spec, _config_, _spec_id, trial, _trial_id = await _persist_spec_trial(service)
    output = _output(spec.variant_id)
    result = _success_result(trial, output, usage=(_usage_reported(in_tok=100, out_tok=50),))
    await service.persist_attempt_result(result)

    # input 200 + output 50 != total 150 → 违反 total = input + output。
    with pytest.raises(IntegrityError):
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "UPDATE eval_llm_call_usages SET input_tokens = 200 "
                    "WHERE execution_id = CAST(:id AS uuid) AND call_index = 0"
                ).bindparams(id=str(result.execution_id))
            )
            await session.commit()


# --------------------------------------------------------------------- replay


async def test_attempt_replay_no_duplicate_usage(service, sessionmaker) -> None:
    spec, _config_, _spec_id, trial, _trial_id = await _persist_spec_trial(service)
    output = _output(spec.variant_id)
    result = _success_result(
        trial, output, usage=(_usage_reported(), _usage_reported(component="audit2"))
    )
    await service.persist_attempt_result(result)
    await service.persist_attempt_result(result)  # 同 execution_id replay

    async with sessionmaker() as session:
        attempts = (
            (
                await session.execute(
                    select(EvalExecutionAttemptModel).where(
                        EvalExecutionAttemptModel.execution_id == result.execution_id
                    )
                )
            )
            .scalars()
            .all()
        )
        usages = (
            (
                await session.execute(
                    select(EvalLlmCallUsageModel).where(
                        EvalLlmCallUsageModel.execution_id == result.execution_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(attempts) == 1
    assert len(usages) == 2


async def test_concurrent_same_attempt_single_row(service, sessionmaker) -> None:
    spec, _config_, _spec_id, trial, _trial_id = await _persist_spec_trial(service)
    output = _output(spec.variant_id)
    result = _success_result(trial, output, usage=(_usage_reported(), _usage_unavailable()))

    await asyncio.gather(
        service.persist_attempt_result(result),
        service.persist_attempt_result(result),
    )

    async with sessionmaker() as session:
        attempts = (
            (
                await session.execute(
                    select(EvalExecutionAttemptModel).where(
                        EvalExecutionAttemptModel.execution_id == result.execution_id
                    )
                )
            )
            .scalars()
            .all()
        )
        usages = (
            (
                await session.execute(
                    select(EvalLlmCallUsageModel).where(
                        EvalLlmCallUsageModel.execution_id == result.execution_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(attempts) == 1
    assert len(usages) == 2


# --------------------------------------------------------------------- verifier 篡改


async def test_verify_tampered_config_payload_rejected(service, sessionmaker) -> None:
    spec, _config_, spec_id, _trial, _trial_id = await _persist_spec_trial(service)
    async with sessionmaker() as session:
        await session.execute(
            text(
                "UPDATE eval_execution_specs SET execution_config_payload = "
                "jsonb_set(execution_config_payload, '{model,temperature}', CAST(:v AS jsonb)) "
                "WHERE execution_spec_id = CAST(:id AS uuid)"
            ).bindparams(v='"1.5"', id=str(spec_id))
        )
        await session.commit()

    with pytest.raises(EvalPersistenceIntegrityError, match="config fingerprint"):
        await service.verify_execution_spec_integrity(spec_id)


async def test_verify_tampered_trial_payload_rejected(service, sessionmaker) -> None:
    spec, _config_, _spec_id, _trial, trial_id = await _persist_spec_trial(service)
    async with sessionmaker() as session:
        await session.execute(
            text(
                "UPDATE eval_trials SET trial_payload = "
                "jsonb_set(trial_payload, '{trial_no}', CAST(:v AS jsonb)) "
                "WHERE trial_id = CAST(:id AS uuid)"
            ).bindparams(v="2", id=str(trial_id))
        )
        await session.commit()

    with pytest.raises(EvalPersistenceIntegrityError, match="trial fingerprint"):
        await service.verify_trial_integrity(trial_id)


async def test_verify_tampered_output_rejected(service, sessionmaker) -> None:
    spec, _config_, _spec_id, trial, _trial_id = await _persist_spec_trial(service)
    output = _output(spec.variant_id)
    result = _success_result(trial, output)
    await service.persist_attempt_result(result)

    async with sessionmaker() as session:
        await session.execute(
            text(
                "UPDATE eval_execution_attempts SET variant_output_payload = "
                "jsonb_set(variant_output_payload, '{final_text}', CAST(:v AS jsonb)) "
                "WHERE execution_id = CAST(:id AS uuid)"
            ).bindparams(v='"tampered"', id=str(result.execution_id))
        )
        await session.commit()

    with pytest.raises(EvalPersistenceIntegrityError, match="output fingerprint"):
        await service.verify_attempt_integrity(result.execution_id)


async def test_verify_usage_call_index_noncontiguous_rejected(service, sessionmaker) -> None:
    spec, _config_, _spec_id, trial, _trial_id = await _persist_spec_trial(service)
    output = _output(spec.variant_id)
    result = _success_result(
        trial, output, usage=(_usage_reported(), _usage_reported(component="audit2"))
    )
    await service.persist_attempt_result(result)

    async with sessionmaker() as session:
        await session.execute(
            text(
                "UPDATE eval_llm_call_usages SET call_index = 5 "
                "WHERE execution_id = CAST(:id AS uuid) AND call_index = 1"
            ).bindparams(id=str(result.execution_id))
        )
        await session.commit()

    with pytest.raises(EvalPersistenceIntegrityError, match="call_index"):
        await service.verify_attempt_integrity(result.execution_id)
