"""SQLAlchemy models for evaluation scoring persistence (stage 7B.1.3B).

镜像 `ExecutionSpec → Trial → Attempt → Output → Scoring` 的评分侧持久化：
- `eval_scoring_specs`：一次评分规格（variant_output_fingerprint +
  human_label_fingerprint + metric_registry_version + judge_config_fingerprint）；
- `eval_score_runs`：一次评分执行（绑定 attempt execution_id + scoring spec）；
- `eval_metric_values`：每条 MetricValue（确定性 / 人工标注 / runtime 指标结果）；
- `eval_human_label_bindings`：immutable 人工标注绑定（label_fingerprint UNIQUE）；
- `eval_judge_runs`：一次 LLM Judge 执行（judge 身份 + config fingerprint +
  usage 汇总 + output fingerprint）；
- `eval_judge_metric_results`：judge 逐指标结果（metric_name UNIQUE per run）。

Variant / Attempt / Score / Judge 严格分离：score_run 只引用 attempt 的
execution_id；judge_run 只引用 score_run。全部行 immutable（create-or-verify +
fingerprint replay；无 update API）。
"""

import uuid
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"
_METRIC_STATUS = "status IN ('computed','not_applicable','unavailable','error')"
_RUN_STATUS = "status IN ('completed','failed')"
_SCORE_RANGE = "score >= 0 AND score <= 1"
_IS_BOOLEAN = "is_required IN (true, false)"


class EvalScoringSpecModel(Base):
    __tablename__ = "eval_scoring_specs"
    __table_args__ = (
        CheckConstraint("schema_version >= 1", name="ck_eval_scoring_specs_schema_version"),
        CheckConstraint(
            f"scoring_spec_fingerprint {_SHA256_CHECK}",
            name="ck_eval_scoring_specs_scoring_spec_fingerprint",
        ),
        CheckConstraint(
            f"variant_output_fingerprint {_SHA256_CHECK}",
            name="ck_eval_scoring_specs_variant_output_fingerprint",
        ),
        CheckConstraint(
            f"human_label_fingerprint IS NULL OR human_label_fingerprint {_SHA256_CHECK}",
            name="ck_eval_scoring_specs_human_label_fingerprint",
        ),
        CheckConstraint(
            f"judge_config_fingerprint IS NULL OR judge_config_fingerprint {_SHA256_CHECK}",
            name="ck_eval_scoring_specs_judge_config_fingerprint",
        ),
        CheckConstraint(
            "metric_registry_version >= 1",
            name="ck_eval_scoring_specs_metric_registry_version",
        ),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_eval_scoring_specs_payload_object",
        ),
        UniqueConstraint(
            "scoring_spec_fingerprint",
            name="uq_eval_scoring_specs_scoring_spec_fingerprint",
        ),
    )

    scoring_spec_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    scoring_spec_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    variant_output_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    human_label_fingerprint: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    metric_registry_version: Mapped[int] = mapped_column(Integer, nullable=False)
    judge_config_fingerprint: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class EvalScoreRunModel(Base):
    __tablename__ = "eval_score_runs"
    __table_args__ = (
        CheckConstraint(_RUN_STATUS, name="ck_eval_score_runs_status"),
        CheckConstraint(
            f"run_fingerprint {_SHA256_CHECK}",
            name="ck_eval_score_runs_run_fingerprint",
        ),
        UniqueConstraint("run_fingerprint", name="uq_eval_score_runs_run_fingerprint"),
        UniqueConstraint(
            "execution_id",
            "scoring_spec_id",
            name="uq_eval_score_runs_exec_spec",
        ),
        Index("ix_eval_score_runs_execution_id", "execution_id"),
    )

    score_run_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    execution_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("eval_execution_attempts.execution_id", ondelete="RESTRICT"),
        nullable=False,
    )
    scoring_spec_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("eval_scoring_specs.scoring_spec_id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    run_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class EvalMetricValueModel(Base):
    __tablename__ = "eval_metric_values"
    __table_args__ = (
        CheckConstraint(_METRIC_STATUS, name="ck_eval_metric_values_status"),
        CheckConstraint(
            "metric_version >= 1",
            name="ck_eval_metric_values_metric_version",
        ),
        CheckConstraint(
            "sample_count >= 0",
            name="ck_eval_metric_values_sample_count",
        ),
        CheckConstraint(
            "denominator IS NULL OR denominator <> 0",
            name="ck_eval_metric_values_denominator_nonzero",
        ),
        CheckConstraint(
            f"metric_value_fingerprint {_SHA256_CHECK}",
            name="ck_eval_metric_values_metric_value_fingerprint",
        ),
        CheckConstraint(
            "value IS NULL OR value >= 0",
            name="ck_eval_metric_values_value_nonnegative",
        ),
        UniqueConstraint(
            "metric_value_fingerprint",
            name="uq_eval_metric_values_metric_value_fingerprint",
        ),
        Index("ix_eval_metric_values_score_run_id", "score_run_id"),
    )

    metric_value_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    score_run_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("eval_score_runs.score_run_id", ondelete="RESTRICT"),
        nullable=False,
    )
    metric_name: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[Decimal | None] = mapped_column(Numeric(38, 12), nullable=True)
    numerator: Mapped[Decimal | None] = mapped_column(Numeric(38, 12), nullable=True)
    denominator: Mapped[Decimal | None] = mapped_column(Numeric(38, 12), nullable=True)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metric_value_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class EvalHumanLabelBindingModel(Base):
    __tablename__ = "eval_human_label_bindings"
    __table_args__ = (
        CheckConstraint("label_schema_version >= 1", name="ck_eval_label_bindings_schema_version"),
        CheckConstraint(
            f"label_fingerprint {_SHA256_CHECK}",
            name="ck_eval_label_bindings_label_fingerprint",
        ),
        UniqueConstraint(
            "label_fingerprint",
            name="uq_eval_label_bindings_label_fingerprint",
        ),
        Index("ix_eval_label_bindings_score_run_id", "score_run_id"),
    )

    binding_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    score_run_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("eval_score_runs.score_run_id", ondelete="RESTRICT"),
        nullable=False,
    )
    label_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    label_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    case_id: Mapped[str] = mapped_column(String(128), nullable=False)
    case_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class EvalJudgeRunModel(Base):
    __tablename__ = "eval_judge_runs"
    __table_args__ = (
        CheckConstraint(_RUN_STATUS, name="ck_eval_judge_runs_status"),
        CheckConstraint(
            f"judge_config_fingerprint {_SHA256_CHECK}",
            name="ck_eval_judge_runs_judge_config_fingerprint",
        ),
        CheckConstraint(
            f"judge_output_fingerprint IS NULL OR judge_output_fingerprint {_SHA256_CHECK}",
            name="ck_eval_judge_runs_judge_output_fingerprint",
        ),
        CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0",
            name="ck_eval_judge_runs_input_tokens",
        ),
        CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0",
            name="ck_eval_judge_runs_output_tokens",
        ),
        CheckConstraint(
            "total_tokens IS NULL OR total_tokens >= 0",
            name="ck_eval_judge_runs_total_tokens",
        ),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_eval_judge_runs_duration_ms",
        ),
        UniqueConstraint(
            "judge_run_fingerprint",
            name="uq_eval_judge_runs_judge_run_fingerprint",
        ),
        Index("ix_eval_judge_runs_score_run_id", "score_run_id"),
    )

    judge_run_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    score_run_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("eval_score_runs.score_run_id", ondelete="RESTRICT"),
        nullable=False,
    )
    judge_name: Mapped[str] = mapped_column(String(64), nullable=False)
    judge_version: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    judge_config_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    judge_run_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    judge_output_fingerprint: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    judge_input_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class EvalJudgeMetricResultModel(Base):
    __tablename__ = "eval_judge_metric_results"
    __table_args__ = (
        CheckConstraint(_METRIC_STATUS, name="ck_eval_judge_metric_results_status"),
        CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 1)",
            name="ck_eval_judge_metric_results_score_range",
        ),
        UniqueConstraint(
            "judge_run_id",
            "metric_name",
            name="uq_eval_judge_metric_results_run_metric",
        ),
        Index("ix_eval_judge_metric_results_judge_run_id", "judge_run_id"),
    )

    judge_metric_result_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    judge_run_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("eval_judge_runs.judge_run_id", ondelete="RESTRICT"),
        nullable=False,
    )
    metric_name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    score: Mapped[Decimal | None] = mapped_column(Numeric(38, 12), nullable=True)
    rationale_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
