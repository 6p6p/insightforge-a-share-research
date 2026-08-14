"""Evaluation scoring persistence service (stage 7B.1.3B).

评分侧持久化（`ExecutionSpec → Trial → Attempt → Output → Scoring`）：
- `create_or_get_scoring_spec`：ScoringSpec 行（fingerprint UNIQUE create-or-get，
  replay 完整重校验）；
- `create_or_get_score_run`：一次评分执行（绑定 attempt `execution_id` +
  scoring spec；`run_fingerprint` UNIQUE）；
- `persist_metric_values`：逐条 MetricValue（create-or-verify，fingerprint
  replay，无 update API）；
- `persist_human_label_binding`：immutable 人工标注绑定（label_fingerprint
  UNIQUE；label 本体由 bundle 承载，不落库）；
- `persist_judge_run` / `persist_judge_metric_results`：judge 执行 + 逐指标结果
  （judge 属 scoring layer，judge config fingerprint 不污染 variant execution
  config）；
- `verify_*_integrity`：加载 → 重校验 → verified read model。

Variant / Attempt / Score / Judge 不可混淆：score_run 只引用 attempt
`execution_id`；judge_run 只引用 score_run。全部 immutable（create-or-verify +
fingerprint replay）。错误消息**不**包含 prompt / output 文本 / token 明细 /
API key / raw JSON。
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.eval_execution import EvalExecutionAttemptModel
from app.db.models.eval_scoring import (
    EvalHumanLabelBindingModel,
    EvalJudgeMetricResultModel,
    EvalJudgeRunModel,
    EvalMetricValueModel,
    EvalScoreRunModel,
    EvalScoringSpecModel,
)
from app.eval.canonical import canonical_json_str
from app.eval.contracts import EvalScoringSpec
from app.eval.errors import (
    EvalPersistenceError,
    EvalPersistenceIntegrityError,
)
from app.eval.fingerprints import (
    compute_scoring_spec_fingerprint,
)
from app.eval.judge.contracts import JudgeConfig, JudgeOutput, JudgeRunOutcome
from app.eval.judge.fingerprints import (
    compute_judge_config_fingerprint,
)
from app.eval.metrics import MetricValue
from app.eval.scoring.contracts import VerifiedMetricValueRecord, VerifiedScoreRunRecord


def _metric_value_fingerprint(value: MetricValue) -> str:
    payload = {
        "metric_name": value.metric_name.value,
        "metric_version": value.metric_version,
        "status": value.status.value,
        "value": str(value.value) if value.value is not None else None,
        "numerator": str(value.numerator) if value.numerator is not None else None,
        "denominator": str(value.denominator) if value.denominator is not None else None,
        "sample_count": value.sample_count,
        "reason_code": value.reason_code,
    }
    return hashlib.sha256(canonical_json_str(payload).encode("utf-8")).hexdigest()


def _score_run_fingerprint(execution_id: UUID, scoring_spec_fingerprint: str) -> str:
    payload = {
        "execution_id": str(execution_id),
        "scoring_spec_fingerprint": scoring_spec_fingerprint,
    }
    return hashlib.sha256(canonical_json_str(payload).encode("utf-8")).hexdigest()


class EvaluationScoringPersistenceService:
    """Persists and verifies evaluation scoring history (0 LLM / 0 network)."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    # ------------------------------------------------------------ scoring spec

    async def create_or_get_scoring_spec(self, spec: EvalScoringSpec) -> tuple[UUID, bool]:
        spec_fp = compute_scoring_spec_fingerprint(spec)
        values = {
            "scoring_spec_id": uuid.uuid4(),
            "schema_version": spec.schema_version,
            "scoring_spec_fingerprint": spec_fp,
            "variant_output_fingerprint": spec.variant_output_fingerprint,
            "human_label_fingerprint": spec.human_label_fingerprint,
            "metric_registry_version": spec.metric_registry_version,
            "judge_config_fingerprint": spec.judge_config_fingerprint,
            "payload": spec.model_dump(mode="json"),
        }
        async with self._sessionmaker() as session:
            async with session.begin():
                stmt = (
                    insert(EvalScoringSpecModel)
                    .values(**values)
                    .on_conflict_do_nothing()
                    .returning(EvalScoringSpecModel)
                )
                row = (await session.execute(stmt)).scalar_one_or_none()
                if row is not None:
                    return row.scoring_spec_id, True
                existing = (
                    await session.execute(
                        select(EvalScoringSpecModel).where(
                            EvalScoringSpecModel.scoring_spec_fingerprint == spec_fp
                        )
                    )
                ).scalar_one_or_none()
                if existing is None:
                    raise EvalPersistenceError("scoring spec 并发冲突后无法回查既有行")
                self._verify_scoring_spec_row(existing)
                return existing.scoring_spec_id, False

    # ------------------------------------------------------------ score run

    async def create_or_get_score_run(
        self,
        execution_id: UUID,
        spec: EvalScoringSpec,
    ) -> tuple[UUID, bool]:
        """评分执行：绑定 attempt execution_id + scoring spec（replay 完整校验）。

        attempt 必须是 success 且其 output fingerprint == spec 绑定的
        `variant_output_fingerprint`（评分只针对实际执行的 output）。
        """
        # scoring spec 先 create-or-get（同 fingerprint replay）。
        spec_id, _ = await self.create_or_get_scoring_spec(spec)
        spec_fp = compute_scoring_spec_fingerprint(spec)
        run_fp = _score_run_fingerprint(execution_id, spec_fp)
        async with self._sessionmaker() as session:
            async with session.begin():
                attempt = (
                    await session.execute(
                        select(EvalExecutionAttemptModel).where(
                            EvalExecutionAttemptModel.execution_id == execution_id
                        )
                    )
                ).scalar_one_or_none()
                if attempt is None:
                    raise EvalPersistenceError("score run 引用的 attempt 不存在")
                if attempt.status != "success":
                    raise EvalPersistenceError("score run 只接受 success attempt")
                if attempt.variant_output_fingerprint != spec.variant_output_fingerprint:
                    raise EvalPersistenceIntegrityError(
                        "score run variant_output_fingerprint 与 attempt 不一致"
                    )
                values = {
                    "score_run_id": uuid.uuid4(),
                    "execution_id": execution_id,
                    "scoring_spec_id": spec_id,
                    "status": "completed",
                    "run_fingerprint": run_fp,
                }
                stmt = (
                    insert(EvalScoreRunModel)
                    .values(**values)
                    .on_conflict_do_nothing()
                    .returning(EvalScoreRunModel)
                )
                row = (await session.execute(stmt)).scalar_one_or_none()
                if row is not None:
                    return row.score_run_id, True
                existing = (
                    await session.execute(
                        select(EvalScoreRunModel).where(EvalScoreRunModel.run_fingerprint == run_fp)
                    )
                ).scalar_one_or_none()
                if existing is None:
                    raise EvalPersistenceError("score run 并发冲突后无法回查既有行")
                return existing.score_run_id, False

    # ------------------------------------------------------------ metric values

    async def persist_metric_values(
        self, score_run_id: UUID, values: tuple[MetricValue, ...]
    ) -> None:
        """逐条 create-or-verify（fingerprint replay；无 update API）。"""
        async with self._sessionmaker() as session:
            async with session.begin():
                run = await session.get(EvalScoreRunModel, score_run_id)
                if run is None:
                    raise EvalPersistenceError("metric values 引用的 score run 不存在")
                for value in values:
                    fp = _metric_value_fingerprint(value)
                    stmt = (
                        insert(EvalMetricValueModel)
                        .values(
                            metric_value_id=uuid.uuid4(),
                            score_run_id=score_run_id,
                            metric_name=value.metric_name.value,
                            metric_version=value.metric_version,
                            status=value.status.value,
                            value=value.value,
                            numerator=value.numerator,
                            denominator=value.denominator,
                            sample_count=value.sample_count,
                            reason_code=value.reason_code,
                            metric_value_fingerprint=fp,
                        )
                        .on_conflict_do_nothing()
                        .returning(EvalMetricValueModel)
                    )
                    row = (await session.execute(stmt)).scalar_one_or_none()
                    if row is not None:
                        continue
                    existing = (
                        await session.execute(
                            select(EvalMetricValueModel).where(
                                EvalMetricValueModel.metric_value_fingerprint == fp
                            )
                        )
                    ).scalar_one_or_none()
                    if existing is None:
                        raise EvalPersistenceError("metric value 并发冲突后无法回查既有行")

    # ------------------------------------------------------------ human label binding

    async def persist_human_label_binding(
        self,
        score_run_id: UUID,
        *,
        label_fingerprint: str,
        label_schema_version: int,
        case_id: str,
        case_version: int,
    ) -> tuple[UUID, bool]:
        """immutable 人工标注绑定（label_fingerprint UNIQUE；label 本体在 bundle）。"""
        async with self._sessionmaker() as session:
            async with session.begin():
                run = await session.get(EvalScoreRunModel, score_run_id)
                if run is None:
                    raise EvalPersistenceError("label binding 引用的 score run 不存在")
                stmt = (
                    insert(EvalHumanLabelBindingModel)
                    .values(
                        binding_id=uuid.uuid4(),
                        score_run_id=score_run_id,
                        label_fingerprint=label_fingerprint,
                        label_schema_version=label_schema_version,
                        case_id=case_id,
                        case_version=case_version,
                    )
                    .on_conflict_do_nothing()
                    .returning(EvalHumanLabelBindingModel)
                )
                row = (await session.execute(stmt)).scalar_one_or_none()
                if row is not None:
                    return row.binding_id, True
                existing = (
                    await session.execute(
                        select(EvalHumanLabelBindingModel).where(
                            EvalHumanLabelBindingModel.label_fingerprint == label_fingerprint
                        )
                    )
                ).scalar_one_or_none()
                if existing is None:
                    raise EvalPersistenceError("label binding 并发冲突后无法回查既有行")
                if (
                    existing.score_run_id != score_run_id
                    or existing.case_id != case_id
                    or existing.case_version != case_version
                ):
                    raise EvalPersistenceIntegrityError("label binding replay 不一致")
                return existing.binding_id, False

    # ------------------------------------------------------------ judge

    async def persist_judge_run(
        self,
        score_run_id: UUID,
        config: JudgeConfig,
        outcome: JudgeRunOutcome,
        judge_input_payload: dict | None = None,
    ) -> tuple[UUID, bool]:
        """一次 judge 执行（config fingerprint + run fingerprint UNIQUE；replay 校验）。"""
        config_fp = compute_judge_config_fingerprint(config)
        run_fp = _judge_run_fingerprint(score_run_id, config_fp)
        values: dict[str, Any] = {
            "judge_run_id": uuid.uuid4(),
            "score_run_id": score_run_id,
            "judge_name": config.judge_name,
            "judge_version": config.judge_version,
            "prompt_version": config.prompt_version,
            "judge_config_fingerprint": config_fp,
            "judge_run_fingerprint": run_fp,
            "provider": config.model.provider,
            "model_id": config.model.model_id,
            "status": outcome.status,
            "error_code": outcome.error_code,
            "input_tokens": outcome.input_tokens,
            "output_tokens": outcome.output_tokens,
            "total_tokens": outcome.total_tokens,
            "duration_ms": outcome.duration_ms,
            "judge_output_fingerprint": outcome.judge_output_fingerprint,
            "judge_input_payload": judge_input_payload,
        }
        async with self._sessionmaker() as session:
            async with session.begin():
                run = await session.get(EvalScoreRunModel, score_run_id)
                if run is None:
                    raise EvalPersistenceError("judge run 引用的 score run 不存在")
                stmt = (
                    insert(EvalJudgeRunModel)
                    .values(**values)
                    .on_conflict_do_nothing()
                    .returning(EvalJudgeRunModel)
                )
                row = (await session.execute(stmt)).scalar_one_or_none()
                if row is not None:
                    return row.judge_run_id, True
                existing = (
                    await session.execute(
                        select(EvalJudgeRunModel).where(
                            EvalJudgeRunModel.judge_run_fingerprint == run_fp
                        )
                    )
                ).scalar_one_or_none()
                if existing is None:
                    raise EvalPersistenceError("judge run 并发冲突后无法回查既有行")
                if (
                    existing.status != outcome.status
                    or existing.judge_output_fingerprint != outcome.judge_output_fingerprint
                ):
                    raise EvalPersistenceIntegrityError("judge run replay 不一致")
                return existing.judge_run_id, False

    async def persist_judge_metric_results(self, judge_run_id: UUID, output: JudgeOutput) -> None:
        """judge 逐指标结果（UNIQUE(judge_run_id, metric_name)；无 update API）。"""
        async with self._sessionmaker() as session:
            async with session.begin():
                run = await session.get(EvalJudgeRunModel, judge_run_id)
                if run is None:
                    raise EvalPersistenceError("judge results 引用的 judge run 不存在")
                for item in output.metric_scores:
                    stmt = (
                        insert(EvalJudgeMetricResultModel)
                        .values(
                            judge_metric_result_id=uuid.uuid4(),
                            judge_run_id=judge_run_id,
                            metric_name=item.metric_name.value,
                            status="computed",
                            score=item.score,
                            rationale_ref=item.rationale_ref,
                        )
                        .on_conflict_do_nothing()
                    )
                    await session.execute(stmt)

    # ------------------------------------------------------------ verifiers

    async def verify_scoring_spec_integrity(self, scoring_spec_id: UUID) -> EvalScoringSpec:
        async with self._sessionmaker() as session:
            row = await session.get(EvalScoringSpecModel, scoring_spec_id)
            if row is None:
                raise EvalPersistenceError("scoring spec not found")
            return self._verify_scoring_spec_row(row)

    async def verify_score_run_integrity(self, score_run_id: UUID) -> VerifiedScoreRunRecord:
        async with self._sessionmaker() as session:
            row = await session.get(EvalScoreRunModel, score_run_id)
            if row is None:
                raise EvalPersistenceError("score run not found")
            spec_row = await session.get(EvalScoringSpecModel, row.scoring_spec_id)
            if spec_row is None:
                raise EvalPersistenceIntegrityError("score run 无父 scoring spec")
            spec = self._verify_scoring_spec_row(spec_row)
            if row.status != "completed":
                raise EvalPersistenceIntegrityError("score run status 非法")
            expected_fp = _score_run_fingerprint(
                row.execution_id, spec_row.scoring_spec_fingerprint
            )
            if row.run_fingerprint != expected_fp:
                raise EvalPersistenceIntegrityError("score run fingerprint 不一致（篡改）")
            metric_rows = (
                (
                    await session.execute(
                        select(EvalMetricValueModel).where(
                            EvalMetricValueModel.score_run_id == score_run_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            values = tuple(
                VerifiedMetricValueRecord(
                    metric_name=row_.metric_name,
                    metric_version=row_.metric_version,
                    status=row_.status,
                    value=row_.value,
                    numerator=row_.numerator,
                    denominator=row_.denominator,
                    sample_count=row_.sample_count,
                    reason_code=row_.reason_code,
                )
                for row_ in metric_rows
            )
            return VerifiedScoreRunRecord(
                score_run_id=score_run_id,
                execution_id=row.execution_id,
                spec=spec,
                metric_values=values,
            )

    def _verify_scoring_spec_row(self, row: EvalScoringSpecModel) -> EvalScoringSpec:
        try:
            spec = EvalScoringSpec.model_validate(row.payload)
        except Exception as exc:  # noqa: BLE001 — pydantic ValidationError
            raise EvalPersistenceIntegrityError("scoring spec payload 非法") from exc
        if compute_scoring_spec_fingerprint(spec) != row.scoring_spec_fingerprint:
            raise EvalPersistenceIntegrityError("scoring spec fingerprint 不一致（篡改）")
        if spec.variant_output_fingerprint != row.variant_output_fingerprint:
            raise EvalPersistenceIntegrityError(
                "scoring spec variant_output_fingerprint 与行不一致"
            )
        if spec.human_label_fingerprint != row.human_label_fingerprint:
            raise EvalPersistenceIntegrityError("scoring spec human_label_fingerprint 与行不一致")
        if spec.metric_registry_version != row.metric_registry_version:
            raise EvalPersistenceIntegrityError("scoring spec metric_registry_version 与行不一致")
        if spec.judge_config_fingerprint != row.judge_config_fingerprint:
            raise EvalPersistenceIntegrityError("scoring spec judge_config_fingerprint 与行不一致")
        return spec


def _judge_run_fingerprint(score_run_id: UUID, judge_config_fingerprint: str) -> str:
    payload = {
        "score_run_id": str(score_run_id),
        "judge_config_fingerprint": judge_config_fingerprint,
    }
    return hashlib.sha256(canonical_json_str(payload).encode("utf-8")).hexdigest()
