"""SQLAlchemy models for evaluation execution persistence (stage 7B.1.3A).

镜像 `ExecutionSpec 1:N Trial 1:N Attempt 1:N LLM Call Usage` 的执行侧持久化
（spec H）：只持久化 ExecutionSpec → Trial → Attempt → LLM Call Usage 四层，
**不**持久化 MetricValue / ScoringSpec / HumanLabel / Judge 结果（spec U）。

- `eval_execution_specs`：一次 variant 执行规格，`execution_spec_fingerprint`
  UNIQUE。同时保存 `execution_spec_payload` + `execution_config_payload`
  （JSONB，完整 frozen contract）；denormalized 列（case / source / config
  fingerprint + variant_id）供查询与快速一致性校验。
- `eval_trials`：同一 spec 的一次复现变体，`trial_fingerprint` UNIQUE +
  UNIQUE(execution_spec_id, trial_no)。`trial_payload` 保存 `EvalTrialSpec`
  canonical 字段。
- `eval_execution_attempts`：trial 内的一次重试，UNIQUE(trial_id, attempt_no)；
  `execution_id` 是 runtime UUID PK（retry = new execution_id + new attempt_no）。
  success/failed 由 CHECK 强制 output fp/payload 与 error_code 的互斥。
- `eval_llm_call_usages`：一次 attempt 的 LLM usage 遥测，UNIQUE(execution_id,
  call_index)。reported/unavailable 由 CHECK 强制三个 token 字段的完整性。

RESTRICT 外键：上游 spec / trial / attempt 存在期间，下游不静默消失。
"""

import uuid
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"

_ATTEMPT_STATUS = "status IN ('success','failed')"
_ATTEMPT_SUCCESS_FIELDS = (
    "(status = 'success' AND variant_output_fingerprint IS NOT NULL "
    "AND variant_output_payload IS NOT NULL AND error_code IS NULL)"
)
_ATTEMPT_FAILED_FIELDS = (
    "(status = 'failed' AND variant_output_fingerprint IS NULL "
    "AND variant_output_payload IS NULL AND error_code IS NOT NULL)"
)

_USAGE_OUTCOME = "outcome IN ('success','parsing_error','invocation_error')"
_USAGE_STATUS = "usage_status IN ('reported','unavailable')"
_USAGE_REPORTED_FIELDS = (
    "(usage_status = 'reported' AND input_tokens IS NOT NULL "
    "AND output_tokens IS NOT NULL AND total_tokens IS NOT NULL "
    "AND input_tokens >= 0 AND output_tokens >= 0 AND total_tokens >= 0 "
    "AND total_tokens = input_tokens + output_tokens)"
)
_USAGE_UNAVAILABLE_FIELDS = (
    "(usage_status = 'unavailable' AND input_tokens IS NULL "
    "AND output_tokens IS NULL AND total_tokens IS NULL)"
)


class EvalExecutionSpecModel(Base):
    __tablename__ = "eval_execution_specs"
    __table_args__ = (
        CheckConstraint("schema_version >= 1", name="ck_eval_exec_specs_schema_version"),
        CheckConstraint(
            f"execution_spec_fingerprint {_SHA256_CHECK}",
            name="ck_eval_exec_specs_execution_spec_fingerprint",
        ),
        CheckConstraint(
            f"case_fingerprint {_SHA256_CHECK}",
            name="ck_eval_exec_specs_case_fingerprint",
        ),
        CheckConstraint(
            f"source_snapshot_fingerprint {_SHA256_CHECK}",
            name="ck_eval_exec_specs_source_snapshot_fingerprint",
        ),
        CheckConstraint(
            f"execution_config_fingerprint {_SHA256_CHECK}",
            name="ck_eval_exec_specs_execution_config_fingerprint",
        ),
        CheckConstraint(
            "jsonb_typeof(execution_spec_payload) = 'object'",
            name="ck_eval_exec_specs_execution_spec_payload_object",
        ),
        CheckConstraint(
            "jsonb_typeof(execution_config_payload) = 'object'",
            name="ck_eval_exec_specs_execution_config_payload_object",
        ),
        UniqueConstraint(
            "execution_spec_fingerprint",
            name="uq_eval_exec_specs_execution_spec_fingerprint",
        ),
        Index("ix_eval_exec_specs_variant_id", "variant_id"),
    )

    execution_spec_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_spec_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    variant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    case_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    source_snapshot_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    execution_config_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    execution_spec_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    execution_config_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class EvalTrialModel(Base):
    __tablename__ = "eval_trials"
    __table_args__ = (
        CheckConstraint("schema_version >= 1", name="ck_eval_trials_schema_version"),
        CheckConstraint("trial_no >= 1", name="ck_eval_trials_trial_no"),
        CheckConstraint(
            f"trial_fingerprint {_SHA256_CHECK}",
            name="ck_eval_trials_trial_fingerprint",
        ),
        CheckConstraint(
            "jsonb_typeof(trial_payload) = 'object'",
            name="ck_eval_trials_trial_payload_object",
        ),
        UniqueConstraint("trial_fingerprint", name="uq_eval_trials_trial_fingerprint"),
        UniqueConstraint(
            "execution_spec_id",
            "trial_no",
            name="uq_eval_trials_spec_trial_no",
        ),
        Index("ix_eval_trials_execution_spec_id", "execution_spec_id"),
    )

    trial_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    execution_spec_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("eval_execution_specs.execution_spec_id", ondelete="RESTRICT"),
        nullable=False,
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    trial_no: Mapped[int] = mapped_column(Integer, nullable=False)
    trial_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    trial_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class EvalExecutionAttemptModel(Base):
    __tablename__ = "eval_execution_attempts"
    __table_args__ = (
        CheckConstraint("attempt_no >= 1", name="ck_eval_exec_attempts_attempt_no"),
        CheckConstraint(_ATTEMPT_STATUS, name="ck_eval_exec_attempts_status"),
        CheckConstraint(
            f"variant_output_fingerprint IS NULL OR variant_output_fingerprint {_SHA256_CHECK}",
            name="ck_eval_exec_attempts_variant_output_fingerprint",
        ),
        CheckConstraint(
            f"{_ATTEMPT_SUCCESS_FIELDS} OR {_ATTEMPT_FAILED_FIELDS}",
            name="ck_eval_exec_attempts_status_fields",
        ),
        CheckConstraint(
            "wall_latency_ms >= 0",
            name="ck_eval_exec_attempts_wall_latency_ms",
        ),
        UniqueConstraint(
            "trial_id",
            "attempt_no",
            name="uq_eval_exec_attempts_trial_attempt_no",
        ),
        Index("ix_eval_exec_attempts_trial_id", "trial_id"),
    )

    execution_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    trial_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("eval_trials.trial_id", ondelete="RESTRICT"),
        nullable=False,
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    wall_latency_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    variant_output_fingerprint: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    variant_output_payload: Mapped[dict | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class EvalLlmCallUsageModel(Base):
    __tablename__ = "eval_llm_call_usages"
    __table_args__ = (
        CheckConstraint("call_index >= 0", name="ck_eval_llm_call_usages_call_index"),
        CheckConstraint(_USAGE_OUTCOME, name="ck_eval_llm_call_usages_outcome"),
        CheckConstraint(_USAGE_STATUS, name="ck_eval_llm_call_usages_usage_status"),
        CheckConstraint(
            "duration_ms >= 0",
            name="ck_eval_llm_call_usages_duration_ms",
        ),
        CheckConstraint(
            f"{_USAGE_REPORTED_FIELDS} OR {_USAGE_UNAVAILABLE_FIELDS}",
            name="ck_eval_llm_call_usages_token_fields",
        ),
        UniqueConstraint(
            "execution_id",
            "call_index",
            name="uq_eval_llm_call_usages_exec_call_index",
        ),
        Index("ix_eval_llm_call_usages_execution_id", "execution_id"),
    )

    usage_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    execution_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("eval_execution_attempts.execution_id", ondelete="RESTRICT"),
        nullable=False,
    )
    call_index: Mapped[int] = mapped_column(Integer, nullable=False)
    component_name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    duration_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    usage_status: Mapped[str] = mapped_column(String(32), nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    input_token_details: Mapped[dict | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    output_token_details: Mapped[dict | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
