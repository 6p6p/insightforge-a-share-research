"""evaluation execution persistence schema

Revision ID: 0045
Revises: 0044
Create Date: 2026-08-13

阶段 7B.1.3A：Evaluation Execution Persistence。

镜像 `ExecutionSpec 1:N Trial 1:N Attempt 1:N LLM Call Usage` 的执行侧持久化
（spec H）：只持久化 ExecutionSpec → Trial → Attempt → LLM Call Usage 四层，
**不**持久化 MetricValue / ScoringSpec / HumanLabel / Judge 结果（spec U），
**不**创建 `eval_runs`。

- `eval_execution_specs`：`execution_spec_fingerprint` UNIQUE，保存
  `execution_spec_payload` + `execution_config_payload`（JSONB）。
- `eval_trials`：`trial_fingerprint` UNIQUE + UNIQUE(execution_spec_id, trial_no)。
- `eval_execution_attempts`：UNIQUE(trial_id, attempt_no)；`execution_id` 是
  runtime UUID PK；CHECK 强制 success/failed 与 output fp/payload、error_code
  互斥。
- `eval_llm_call_usages`：UNIQUE(execution_id, call_index)；CHECK 强制
  reported/unavailable 与三个 token 字段完整性。

**downgrade guard**：四张表任一存在行 → 拒绝回滚。执行历史是正式 immutable
research/eval artifact，不在 downgrade 时静默删除；alembic_version 保持 0045。
四张表全部为空时才允许回到 0044。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SPECS = "eval_execution_specs"
_TRIALS = "eval_trials"
_ATTEMPTS = "eval_execution_attempts"
_USAGES = "eval_llm_call_usages"

_TABLES = (_SPECS, _TRIALS, _ATTEMPTS, _USAGES)

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


def _table_has_row(table: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1"))
    return rows.first() is not None


def upgrade() -> None:
    op.create_table(
        _SPECS,
        sa.Column(
            "execution_spec_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("execution_spec_fingerprint", sa.CHAR(64), nullable=False),
        sa.Column("variant_id", sa.String(32), nullable=False),
        sa.Column("case_fingerprint", sa.CHAR(64), nullable=False),
        sa.Column("source_snapshot_fingerprint", sa.CHAR(64), nullable=False),
        sa.Column("execution_config_fingerprint", sa.CHAR(64), nullable=False),
        sa.Column("execution_spec_payload", postgresql.JSONB(), nullable=False),
        sa.Column("execution_config_payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("execution_spec_id", name="pk_eval_execution_specs"),
        sa.CheckConstraint(
            "schema_version >= 1",
            name="ck_eval_exec_specs_schema_version",
        ),
        sa.CheckConstraint(
            f"execution_spec_fingerprint {_SHA256_CHECK}",
            name="ck_eval_exec_specs_execution_spec_fingerprint",
        ),
        sa.CheckConstraint(
            f"case_fingerprint {_SHA256_CHECK}",
            name="ck_eval_exec_specs_case_fingerprint",
        ),
        sa.CheckConstraint(
            f"source_snapshot_fingerprint {_SHA256_CHECK}",
            name="ck_eval_exec_specs_source_snapshot_fingerprint",
        ),
        sa.CheckConstraint(
            f"execution_config_fingerprint {_SHA256_CHECK}",
            name="ck_eval_exec_specs_execution_config_fingerprint",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(execution_spec_payload) = 'object'",
            name="ck_eval_exec_specs_execution_spec_payload_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(execution_config_payload) = 'object'",
            name="ck_eval_exec_specs_execution_config_payload_object",
        ),
        sa.UniqueConstraint(
            "execution_spec_fingerprint",
            name="uq_eval_exec_specs_execution_spec_fingerprint",
        ),
    )
    op.create_index("ix_eval_exec_specs_variant_id", _SPECS, ["variant_id"])

    op.create_table(
        _TRIALS,
        sa.Column(
            "trial_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "execution_spec_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("eval_execution_specs.execution_spec_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("trial_no", sa.Integer(), nullable=False),
        sa.Column("trial_fingerprint", sa.CHAR(64), nullable=False),
        sa.Column("trial_payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("trial_id", name="pk_eval_trials"),
        sa.CheckConstraint("schema_version >= 1", name="ck_eval_trials_schema_version"),
        sa.CheckConstraint("trial_no >= 1", name="ck_eval_trials_trial_no"),
        sa.CheckConstraint(
            f"trial_fingerprint {_SHA256_CHECK}",
            name="ck_eval_trials_trial_fingerprint",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(trial_payload) = 'object'",
            name="ck_eval_trials_trial_payload_object",
        ),
        sa.UniqueConstraint("trial_fingerprint", name="uq_eval_trials_trial_fingerprint"),
        sa.UniqueConstraint(
            "execution_spec_id",
            "trial_no",
            name="uq_eval_trials_spec_trial_no",
        ),
    )
    op.create_index("ix_eval_trials_execution_spec_id", _TRIALS, ["execution_spec_id"])

    op.create_table(
        _ATTEMPTS,
        sa.Column(
            "execution_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "trial_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("eval_trials.trial_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("wall_latency_ms", sa.BigInteger(), nullable=False),
        sa.Column("variant_output_fingerprint", sa.CHAR(64), nullable=True),
        sa.Column("variant_output_payload", postgresql.JSONB(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("execution_id", name="pk_eval_execution_attempts"),
        sa.CheckConstraint("attempt_no >= 1", name="ck_eval_exec_attempts_attempt_no"),
        sa.CheckConstraint(_ATTEMPT_STATUS, name="ck_eval_exec_attempts_status"),
        sa.CheckConstraint(
            f"variant_output_fingerprint IS NULL OR variant_output_fingerprint {_SHA256_CHECK}",
            name="ck_eval_exec_attempts_variant_output_fingerprint",
        ),
        sa.CheckConstraint(
            f"{_ATTEMPT_SUCCESS_FIELDS} OR {_ATTEMPT_FAILED_FIELDS}",
            name="ck_eval_exec_attempts_status_fields",
        ),
        sa.CheckConstraint(
            "wall_latency_ms >= 0",
            name="ck_eval_exec_attempts_wall_latency_ms",
        ),
        sa.UniqueConstraint(
            "trial_id",
            "attempt_no",
            name="uq_eval_exec_attempts_trial_attempt_no",
        ),
    )
    op.create_index("ix_eval_exec_attempts_trial_id", _ATTEMPTS, ["trial_id"])

    op.create_table(
        _USAGES,
        sa.Column(
            "usage_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "execution_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("eval_execution_attempts.execution_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("call_index", sa.Integer(), nullable=False),
        sa.Column("component_name", sa.String(128), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model_id", sa.String(128), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("duration_ms", sa.BigInteger(), nullable=False),
        sa.Column("usage_status", sa.String(32), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("total_tokens", sa.BigInteger(), nullable=True),
        sa.Column("input_token_details", postgresql.JSONB(), nullable=True),
        sa.Column("output_token_details", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("usage_id", name="pk_eval_llm_call_usages"),
        sa.CheckConstraint("call_index >= 0", name="ck_eval_llm_call_usages_call_index"),
        sa.CheckConstraint(_USAGE_OUTCOME, name="ck_eval_llm_call_usages_outcome"),
        sa.CheckConstraint(_USAGE_STATUS, name="ck_eval_llm_call_usages_usage_status"),
        sa.CheckConstraint(
            "duration_ms >= 0",
            name="ck_eval_llm_call_usages_duration_ms",
        ),
        sa.CheckConstraint(
            f"{_USAGE_REPORTED_FIELDS} OR {_USAGE_UNAVAILABLE_FIELDS}",
            name="ck_eval_llm_call_usages_token_fields",
        ),
        sa.UniqueConstraint(
            "execution_id",
            "call_index",
            name="uq_eval_llm_call_usages_exec_call_index",
        ),
    )
    op.create_index("ix_eval_llm_call_usages_execution_id", _USAGES, ["execution_id"])


def downgrade() -> None:
    # 数据安全：执行历史是正式 immutable eval artifact（记录了 ExecutionSpec →
    # Trial → Attempt → LLM Call Usage 的完整执行链路），不在 downgrade 时静默
    # 删除历史。四张表任一存在行 → 拒绝回滚（不删除数据 / 不修改行），
    # alembic_version 保持 0045。四张表全部为空时才允许回到 0044。
    for table in _TABLES:
        if _table_has_row(table):
            raise RuntimeError(
                f"cannot downgrade migration 0045: rows present in {table}; "
                "refusing to drop evaluation execution history "
                "(alembic_version stays 0045)"
            )
    for table in reversed(_TABLES):
        op.drop_table(table)
