"""Evaluation execution persistence service (stage 7B.1.3A).

持久化 `ExecutionSpec → Trial → Attempt → LLM Call Usage` 四层（spec H）：
- `create_or_get_execution_spec` / `create_or_get_trial`：ON CONFLICT DO NOTHING
  的 create-or-get（无 Python 进程锁，UNIQUE 是并发唯一性来源），replay 时完整
  重校验 + 重算 fingerprint，一致 = replay，不一致 = `EvalPersistenceIntegrityError`。
- `persist_attempt_result`：单事务写 attempt 行 + N usage 行，失败全回滚；
  同 execution_id / (trial_id, attempt_no) 重放 → 完整校验，静默覆盖禁止。
- `verify_execution_spec_integrity` / `verify_trial_integrity` /
  `verify_attempt_integrity`：加载 → `model_validate`（绝不 `model_construct`，
  不信任 DB JSONB）→ 重算 config/spec/trial/output fingerprint → 校验 → 返回
  verified read model；usage 按 call_index 排序、连续、逐条重建
  `LlmCallUsageRecord`。

错误消息**不**包含 prompt / output 文本 / token 明细 payload / API key / raw JSON。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.eval_execution import (
    EvalExecutionAttemptModel,
    EvalExecutionSpecModel,
    EvalLlmCallUsageModel,
    EvalTrialModel,
)
from app.eval.contracts import EvalExecutionConfig, EvalExecutionSpec, EvalVariantOutput
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
from app.eval.persistence.contracts import (
    VerifiedAttemptRecord,
    VerifiedExecutionSpecRecord,
    VerifiedTrialRecord,
)
from app.llm.instrumentation import LlmCallOutcome, LlmCallUsageRecord, UsageStatus


def _trial_payload(trial_spec: EvalTrialSpec) -> dict:
    return {
        "schema_version": trial_spec.schema_version,
        "execution_spec_fingerprint": trial_spec.execution_spec_fingerprint,
        "trial_no": trial_spec.trial_no,
    }


class EvaluationExecutionPersistenceService:
    """Persists and verifies evaluation execution history (0 LLM / 0 network)。"""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    # ------------------------------------------------------------ create-or-get

    async def create_or_get_execution_spec(
        self, spec: EvalExecutionSpec, config: EvalExecutionConfig
    ) -> tuple[UUID, bool]:
        """创建或取回 execution spec（config 一致性先校验；replay 完整校验）。"""
        config_fp = compute_execution_config_fingerprint(config)
        if config_fp != spec.execution_config_fingerprint:
            raise EvalPersistenceIntegrityError(
                "execution config fingerprint 与 execution_spec 不一致"
            )
        if config.variant_id != spec.variant_id:
            raise EvalPersistenceIntegrityError(
                "config.variant_id 与 execution_spec.variant_id 不一致"
            )
        spec_fp = compute_execution_spec_fingerprint(spec)
        values = {
            "schema_version": spec.schema_version,
            "execution_spec_fingerprint": spec_fp,
            "variant_id": spec.variant_id.value,
            "case_fingerprint": spec.case_fingerprint,
            "source_snapshot_fingerprint": spec.source_snapshot_fingerprint,
            "execution_config_fingerprint": spec.execution_config_fingerprint,
            "execution_spec_payload": spec.model_dump(mode="json"),
            "execution_config_payload": config.model_dump(mode="json"),
        }
        async with self._sessionmaker() as session:
            async with session.begin():
                stmt = (
                    insert(EvalExecutionSpecModel)
                    .values(**values)
                    .on_conflict_do_nothing()
                    .returning(EvalExecutionSpecModel)
                )
                row = (await session.execute(stmt)).scalar_one_or_none()
                if row is not None:
                    return row.execution_spec_id, True
                existing = (
                    await session.execute(
                        select(EvalExecutionSpecModel).where(
                            EvalExecutionSpecModel.execution_spec_fingerprint == spec_fp
                        )
                    )
                ).scalar_one_or_none()
                if existing is None:
                    raise EvalPersistenceError("execution spec 并发冲突后无法回查既有行")
                self._verify_spec_row(existing)
                return existing.execution_spec_id, False

    async def create_or_get_trial(
        self, execution_spec_id: UUID, trial_spec: EvalTrialSpec
    ) -> tuple[UUID, bool]:
        """创建或取回 trial（父 spec 身份 + trial fingerprint 先校验）。"""
        trial_fp = compute_trial_fingerprint(trial_spec)
        values = {
            "execution_spec_id": execution_spec_id,
            "schema_version": trial_spec.schema_version,
            "trial_no": trial_spec.trial_no,
            "trial_fingerprint": trial_fp,
            "trial_payload": _trial_payload(trial_spec),
        }
        async with self._sessionmaker() as session:
            async with session.begin():
                parent = (
                    await session.execute(
                        select(EvalExecutionSpecModel).where(
                            EvalExecutionSpecModel.execution_spec_id == execution_spec_id
                        )
                    )
                ).scalar_one_or_none()
                if parent is None:
                    raise EvalPersistenceError("execution spec not found")
                if trial_spec.execution_spec_fingerprint != parent.execution_spec_fingerprint:
                    raise EvalPersistenceIntegrityError(
                        "trial.execution_spec_fingerprint 与父 spec 不一致"
                    )
                stmt = (
                    insert(EvalTrialModel)
                    .values(**values)
                    .on_conflict_do_nothing()
                    .returning(EvalTrialModel)
                )
                row = (await session.execute(stmt)).scalar_one_or_none()
                if row is not None:
                    return row.trial_id, True
                existing = (
                    await session.execute(
                        select(EvalTrialModel).where(EvalTrialModel.trial_fingerprint == trial_fp)
                    )
                ).scalar_one_or_none()
                if existing is None:
                    raise EvalPersistenceError("trial 并发冲突后无法回查既有行")
                self._verify_trial_row(existing, parent)
                return existing.trial_id, False

    # ------------------------------------------------------------ attempt

    async def persist_attempt_result(self, result: EvalExecutionAttemptResult) -> UUID:
        """单事务持久化一次 attempt + N usage（失败全回滚；replay 完整校验）。"""
        async with self._sessionmaker() as session:
            async with session.begin():
                trial = (
                    await session.execute(
                        select(EvalTrialModel).where(
                            EvalTrialModel.trial_fingerprint == result.trial_fingerprint
                        )
                    )
                ).scalar_one_or_none()
                if trial is None:
                    raise EvalPersistenceIntegrityError("attempt 引用的 trial 不存在")
                parent = (
                    await session.execute(
                        select(EvalExecutionSpecModel).where(
                            EvalExecutionSpecModel.execution_spec_id == trial.execution_spec_id
                        )
                    )
                ).scalar_one_or_none()
                if parent is None:
                    raise EvalPersistenceIntegrityError("attempt 引用的 trial 无父 spec")
                if parent.variant_id != result.variant_id.value:
                    raise EvalPersistenceIntegrityError("attempt variant_id 与父 spec 不一致")

                output_payload: dict | None = None
                if result.status == ExecutionAttemptStatus.SUCCESS:
                    output = result.variant_output
                    assert output is not None
                    output_payload = output.model_dump(mode="json")
                    revalidated = EvalVariantOutput.model_validate(output_payload)
                    if (
                        compute_variant_output_fingerprint(revalidated)
                        != result.variant_output_fingerprint
                    ):
                        raise EvalPersistenceIntegrityError("variant output fingerprint 不一致")
                    if revalidated.variant_id != result.variant_id:
                        raise EvalPersistenceIntegrityError("variant output variant_id 不一致")
                    if (
                        revalidated.case_id != result.case_id
                        or revalidated.case_version != result.case_version
                    ):
                        raise EvalPersistenceIntegrityError("variant output case identity 不一致")

                attempt_values = {
                    "execution_id": result.execution_id,
                    "trial_id": trial.trial_id,
                    "attempt_no": result.attempt_no,
                    "status": result.status.value,
                    "wall_latency_ms": result.wall_latency_ms,
                    "variant_output_fingerprint": result.variant_output_fingerprint,
                    "variant_output_payload": output_payload,
                    "error_code": result.error_code,
                }
                stmt = (
                    insert(EvalExecutionAttemptModel)
                    .values(**attempt_values)
                    .on_conflict_do_nothing()
                    .returning(EvalExecutionAttemptModel)
                )
                row = (await session.execute(stmt)).scalar_one_or_none()
                if row is not None:
                    for call_index, record in enumerate(result.usage_records):
                        await session.execute(
                            insert(EvalLlmCallUsageModel)
                            .values(**self._usage_values(result.execution_id, call_index, record))
                            .on_conflict_do_nothing()
                        )
                    return result.execution_id

                existing = (
                    await session.execute(
                        select(EvalExecutionAttemptModel).where(
                            EvalExecutionAttemptModel.execution_id == result.execution_id
                        )
                    )
                ).scalar_one_or_none()
                if existing is None:
                    existing = (
                        await session.execute(
                            select(EvalExecutionAttemptModel).where(
                                EvalExecutionAttemptModel.trial_id == trial.trial_id,
                                EvalExecutionAttemptModel.attempt_no == result.attempt_no,
                            )
                        )
                    ).scalar_one_or_none()
                if existing is None:
                    raise EvalPersistenceError("attempt 并发冲突后无法回查既有行")
                verified = await self._verify_attempt_row(session, existing, trial, parent)
                self._assert_attempt_replay_matches(verified, result)
                return result.execution_id

    # ------------------------------------------------------------ verifiers

    async def verify_execution_spec_integrity(
        self, execution_spec_id: UUID
    ) -> VerifiedExecutionSpecRecord:
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    select(EvalExecutionSpecModel).where(
                        EvalExecutionSpecModel.execution_spec_id == execution_spec_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise EvalPersistenceError("execution spec not found")
            return self._verify_spec_row(row)

    async def verify_trial_integrity(self, trial_id: UUID) -> VerifiedTrialRecord:
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    select(EvalTrialModel).where(EvalTrialModel.trial_id == trial_id)
                )
            ).scalar_one_or_none()
            if row is None:
                raise EvalPersistenceError("trial not found")
            parent = (
                await session.execute(
                    select(EvalExecutionSpecModel).where(
                        EvalExecutionSpecModel.execution_spec_id == row.execution_spec_id
                    )
                )
            ).scalar_one_or_none()
            if parent is None:
                raise EvalPersistenceIntegrityError("trial 无父 spec")
            return self._verify_trial_row(row, parent)

    async def verify_attempt_integrity(self, execution_id: UUID) -> VerifiedAttemptRecord:
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    select(EvalExecutionAttemptModel).where(
                        EvalExecutionAttemptModel.execution_id == execution_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise EvalPersistenceError("attempt not found")
            trial = (
                await session.execute(
                    select(EvalTrialModel).where(EvalTrialModel.trial_id == row.trial_id)
                )
            ).scalar_one_or_none()
            if trial is None:
                raise EvalPersistenceIntegrityError("attempt 无父 trial")
            parent = (
                await session.execute(
                    select(EvalExecutionSpecModel).where(
                        EvalExecutionSpecModel.execution_spec_id == trial.execution_spec_id
                    )
                )
            ).scalar_one_or_none()
            if parent is None:
                raise EvalPersistenceIntegrityError("attempt 无祖父 spec")
            return await self._verify_attempt_row(session, row, trial, parent)

    # ------------------------------------------------------------ row verification

    def _verify_spec_row(self, row: EvalExecutionSpecModel) -> VerifiedExecutionSpecRecord:
        try:
            spec = EvalExecutionSpec.model_validate(row.execution_spec_payload)
            config = EvalExecutionConfig.model_validate(row.execution_config_payload)
        except Exception as exc:  # noqa: BLE001 — pydantic ValidationError
            raise EvalPersistenceIntegrityError("execution spec/config payload 非法") from exc
        if spec.variant_id.value != row.variant_id:
            raise EvalPersistenceIntegrityError("spec.variant_id 与行 variant_id 不一致")
        if config.variant_id != spec.variant_id:
            raise EvalPersistenceIntegrityError("config.variant_id 与 spec.variant_id 不一致")
        if compute_execution_config_fingerprint(config) != row.execution_config_fingerprint:
            raise EvalPersistenceIntegrityError("execution config fingerprint 不一致（篡改）")
        if spec.execution_config_fingerprint != row.execution_config_fingerprint:
            raise EvalPersistenceIntegrityError("spec.execution_config_fingerprint 与行不一致")
        if spec.case_fingerprint != row.case_fingerprint:
            raise EvalPersistenceIntegrityError("spec.case_fingerprint 与行不一致")
        if spec.source_snapshot_fingerprint != row.source_snapshot_fingerprint:
            raise EvalPersistenceIntegrityError("spec.source_snapshot_fingerprint 与行不一致")
        if spec.schema_version != row.schema_version:
            raise EvalPersistenceIntegrityError("spec.schema_version 与行不一致")
        if compute_execution_spec_fingerprint(spec) != row.execution_spec_fingerprint:
            raise EvalPersistenceIntegrityError("execution spec fingerprint 不一致（篡改）")
        return VerifiedExecutionSpecRecord(
            execution_spec_id=row.execution_spec_id, spec=spec, config=config
        )

    def _verify_trial_row(
        self, row: EvalTrialModel, parent: EvalExecutionSpecModel
    ) -> VerifiedTrialRecord:
        payload = row.trial_payload
        try:
            trial_spec = EvalTrialSpec(
                execution_spec_fingerprint=payload["execution_spec_fingerprint"],
                trial_no=payload["trial_no"],
                schema_version=payload["schema_version"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EvalPersistenceIntegrityError("trial payload 非法") from exc
        if trial_spec.execution_spec_fingerprint != parent.execution_spec_fingerprint:
            raise EvalPersistenceIntegrityError("trial.execution_spec_fingerprint 与父 spec 不一致")
        if row.execution_spec_id != parent.execution_spec_id:
            raise EvalPersistenceIntegrityError("trial.execution_spec_id 与父 spec 不一致")
        if compute_trial_fingerprint(trial_spec) != row.trial_fingerprint:
            raise EvalPersistenceIntegrityError("trial fingerprint 不一致（篡改）")
        if trial_spec.trial_no != row.trial_no:
            raise EvalPersistenceIntegrityError("trial.trial_no 与行不一致")
        if trial_spec.schema_version != row.schema_version:
            raise EvalPersistenceIntegrityError("trial.schema_version 与行不一致")
        return VerifiedTrialRecord(
            trial_id=row.trial_id, execution_spec_id=row.execution_spec_id, trial_spec=trial_spec
        )

    async def _verify_attempt_row(
        self,
        session: AsyncSession,
        row: EvalExecutionAttemptModel,
        trial: EvalTrialModel,
        parent: EvalExecutionSpecModel,
    ) -> VerifiedAttemptRecord:
        verified_spec = self._verify_spec_row(parent)
        self._verify_trial_row(trial, parent)
        if row.trial_id != trial.trial_id:
            raise EvalPersistenceIntegrityError("attempt.trial_id 与 trial 不一致")
        if row.status not in ("success", "failed"):
            raise EvalPersistenceIntegrityError("attempt status 非法")
        status = ExecutionAttemptStatus(row.status)
        if row.wall_latency_ms < 0:
            raise EvalPersistenceIntegrityError("attempt wall_latency_ms 非法")

        output: EvalVariantOutput | None = None
        if status == ExecutionAttemptStatus.SUCCESS:
            if row.variant_output_payload is None or row.variant_output_fingerprint is None:
                raise EvalPersistenceIntegrityError("success attempt 缺 output payload/fingerprint")
            if row.error_code is not None:
                raise EvalPersistenceIntegrityError("success attempt 携带 error_code")
            try:
                output = EvalVariantOutput.model_validate(row.variant_output_payload)
            except Exception as exc:  # noqa: BLE001 — pydantic ValidationError
                raise EvalPersistenceIntegrityError("variant output payload 非法") from exc
            if compute_variant_output_fingerprint(output) != row.variant_output_fingerprint:
                raise EvalPersistenceIntegrityError("variant output fingerprint 不一致（篡改）")
            if output.variant_id != verified_spec.spec.variant_id:
                raise EvalPersistenceIntegrityError("variant output variant_id 与父 spec 不一致")
        else:
            if row.variant_output_payload is not None or row.variant_output_fingerprint is not None:
                raise EvalPersistenceIntegrityError("failed attempt 携带 output")
            if not row.error_code or not row.error_code.strip():
                raise EvalPersistenceIntegrityError("failed attempt 缺 error_code")

        usage_rows = (
            (
                await session.execute(
                    select(EvalLlmCallUsageModel)
                    .where(EvalLlmCallUsageModel.execution_id == row.execution_id)
                    .order_by(EvalLlmCallUsageModel.call_index)
                )
            )
            .scalars()
            .all()
        )
        records: list[LlmCallUsageRecord] = []
        for expected_index, urow in enumerate(usage_rows):
            if urow.call_index != expected_index:
                raise EvalPersistenceIntegrityError("usage call_index 不连续")
            records.append(self._usage_record_from_row(urow))
        return VerifiedAttemptRecord(
            execution_id=row.execution_id,
            trial_id=row.trial_id,
            attempt_no=row.attempt_no,
            status=status,
            wall_latency_ms=row.wall_latency_ms,
            variant_output=output,
            variant_output_fingerprint=row.variant_output_fingerprint,
            error_code=row.error_code,
            usage_records=tuple(records),
        )

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _usage_values(execution_id: UUID, call_index: int, record: LlmCallUsageRecord) -> dict:
        return {
            "execution_id": execution_id,
            "call_index": call_index,
            "component_name": record.component_name,
            "provider": record.provider,
            "model_id": record.model_id,
            "outcome": record.outcome.value,
            "duration_ms": record.duration_ms,
            "usage_status": record.usage_status.value,
            "input_tokens": record.input_tokens,
            "output_tokens": record.output_tokens,
            "total_tokens": record.total_tokens,
            "input_token_details": record.input_token_details,
            "output_token_details": record.output_token_details,
        }

    @staticmethod
    def _usage_record_from_row(row: EvalLlmCallUsageModel) -> LlmCallUsageRecord:
        try:
            return LlmCallUsageRecord(
                component_name=row.component_name,
                provider=row.provider,
                model_id=row.model_id,
                outcome=LlmCallOutcome(row.outcome),
                duration_ms=row.duration_ms,
                usage_status=UsageStatus(row.usage_status),
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                total_tokens=row.total_tokens,
                input_token_details=row.input_token_details,
                output_token_details=row.output_token_details,
            )
        except (ValueError, KeyError) as exc:
            raise EvalPersistenceIntegrityError("usage 行契约非法") from exc

    @staticmethod
    def _assert_attempt_replay_matches(
        verified: VerifiedAttemptRecord, result: EvalExecutionAttemptResult
    ) -> None:
        if verified.execution_id != result.execution_id:
            raise EvalPersistenceIntegrityError("attempt replay execution_id 不一致")
        if verified.attempt_no != result.attempt_no:
            raise EvalPersistenceIntegrityError("attempt replay attempt_no 不一致")
        if verified.status != result.status:
            raise EvalPersistenceIntegrityError("attempt replay status 不一致")
        if verified.wall_latency_ms != result.wall_latency_ms:
            raise EvalPersistenceIntegrityError("attempt replay wall_latency_ms 不一致")
        if verified.variant_output_fingerprint != result.variant_output_fingerprint:
            raise EvalPersistenceIntegrityError("attempt replay variant_output_fingerprint 不一致")
        if verified.error_code != result.error_code:
            raise EvalPersistenceIntegrityError("attempt replay error_code 不一致")
        if result.status == ExecutionAttemptStatus.SUCCESS:
            assert verified.variant_output is not None
            if verified.variant_output.variant_id != result.variant_id:
                raise EvalPersistenceIntegrityError("attempt replay variant_id 不一致")
            if verified.variant_output.case_id != result.case_id:
                raise EvalPersistenceIntegrityError("attempt replay case_id 不一致")
            if verified.variant_output.case_version != result.case_version:
                raise EvalPersistenceIntegrityError("attempt replay case_version 不一致")
        if len(verified.usage_records) != len(result.usage_records):
            raise EvalPersistenceIntegrityError("attempt replay usage 数量不一致")
        for persisted, original in zip(verified.usage_records, result.usage_records, strict=True):
            if persisted != original:
                raise EvalPersistenceIntegrityError("attempt replay usage 内容不一致")
