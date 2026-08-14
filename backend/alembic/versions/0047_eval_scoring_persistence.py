"""evaluation scoring persistence schema

Revision ID: 0047
Revises: 0046
Create Date: 2026-08-14

阶段 7B.1.3B：Evaluation Scoring Persistence。

镜像 `ExecutionSpec → Trial → Attempt → Output → Scoring` 的评分侧持久化
（spec U），与执行侧（0045）严格分离：

- `eval_scoring_specs`：ScoringSpec 行（`scoring_spec_fingerprint` UNIQUE，
  保存 variant_output_fingerprint / human_label_fingerprint /
  metric_registry_version / judge_config_fingerprint + payload JSONB）；
- `eval_score_runs`：一次评分执行（绑定 attempt `execution_id` + scoring spec；
  `run_fingerprint` UNIQUE + UNIQUE(execution_id, scoring_spec_id)）；
- `eval_metric_values`：每条 MetricValue（metric_name + status + value /
  numerator / denominator + reason_code；`metric_value_fingerprint` UNIQUE）；
- `eval_human_label_bindings`：immutable 人工标注绑定（`label_fingerprint`
  UNIQUE，label 本体由 bundle 承载，不落库）；
- `eval_judge_runs`：一次 LLM Judge 执行（judge 身份 + config fingerprint +
  usage 汇总 + `judge_output_fingerprint`；`judge_run_fingerprint` UNIQUE）；
- `eval_judge_metric_results`：judge 逐指标结果（UNIQUE(judge_run_id,
  metric_name)；score ∈ [0,1]）。

Variant / Attempt / Score / Judge 不可混淆：score_run 只引用 attempt 的
execution_id；judge_run 只引用 score_run。全部行 immutable（create-or-verify +
fingerprint replay；无 update API）。

**downgrade guard**：六张表任一存在行 → 拒绝回滚。评分历史是正式 immutable
eval artifact，不在 downgrade 时静默删除；alembic_version 保持 0047。全部为空
时才允许回到 0046。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SPECS = "eval_scoring_specs"
_RUNS = "eval_score_runs"
_VALUES = "eval_metric_values"
_BINDINGS = "eval_human_label_bindings"
_JUDGE_RUNS = "eval_judge_runs"
_JUDGE_RESULTS = "eval_judge_metric_results"

_TABLES = (_SPECS, _RUNS, _VALUES, _BINDINGS, _JUDGE_RUNS, _JUDGE_RESULTS)

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"
_METRIC_STATUS = "status IN ('computed','not_applicable','unavailable','error')"
_RUN_STATUS = "status IN ('completed','failed')"


def _table_has_row(table: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1"))
    return rows.first() is not None


def upgrade() -> None:
    op.create_table(
        _SPECS,
        sa.Column("scoring_spec_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("scoring_spec_fingerprint", sa.CHAR(64), nullable=False),
        sa.Column("variant_output_fingerprint", sa.CHAR(64), nullable=False),
        sa.Column("human_label_fingerprint", sa.CHAR(64), nullable=True),
        sa.Column("metric_registry_version", sa.Integer(), nullable=False),
        sa.Column("judge_config_fingerprint", sa.CHAR(64), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("scoring_spec_id", name="pk_eval_scoring_specs"),
        sa.CheckConstraint("schema_version >= 1", name="ck_eval_scoring_specs_schema_version"),
        sa.CheckConstraint(
            f"scoring_spec_fingerprint {_SHA256_CHECK}",
            name="ck_eval_scoring_specs_scoring_spec_fingerprint",
        ),
        sa.CheckConstraint(
            f"variant_output_fingerprint {_SHA256_CHECK}",
            name="ck_eval_scoring_specs_variant_output_fingerprint",
        ),
        sa.CheckConstraint(
            f"human_label_fingerprint IS NULL OR human_label_fingerprint {_SHA256_CHECK}",
            name="ck_eval_scoring_specs_human_label_fingerprint",
        ),
        sa.CheckConstraint(
            f"judge_config_fingerprint IS NULL OR judge_config_fingerprint {_SHA256_CHECK}",
            name="ck_eval_scoring_specs_judge_config_fingerprint",
        ),
        sa.CheckConstraint(
            "metric_registry_version >= 1",
            name="ck_eval_scoring_specs_metric_registry_version",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_eval_scoring_specs_payload_object",
        ),
        sa.UniqueConstraint(
            "scoring_spec_fingerprint",
            name="uq_eval_scoring_specs_scoring_spec_fingerprint",
        ),
    )

    op.create_table(
        _RUNS,
        sa.Column("score_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "execution_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("eval_execution_attempts.execution_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "scoring_spec_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("eval_scoring_specs.scoring_spec_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("run_fingerprint", sa.CHAR(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("score_run_id", name="pk_eval_score_runs"),
        sa.CheckConstraint(_RUN_STATUS, name="ck_eval_score_runs_status"),
        sa.CheckConstraint(
            f"run_fingerprint {_SHA256_CHECK}",
            name="ck_eval_score_runs_run_fingerprint",
        ),
        sa.UniqueConstraint("run_fingerprint", name="uq_eval_score_runs_run_fingerprint"),
        sa.UniqueConstraint(
            "execution_id",
            "scoring_spec_id",
            name="uq_eval_score_runs_exec_spec",
        ),
    )
    op.create_index("ix_eval_score_runs_execution_id", _RUNS, ["execution_id"])

    op.create_table(
        _VALUES,
        sa.Column("metric_value_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "score_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("eval_score_runs.score_run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("metric_name", sa.String(64), nullable=False),
        sa.Column("metric_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("value", sa.Numeric(38, 12), nullable=True),
        sa.Column("numerator", sa.Numeric(38, 12), nullable=True),
        sa.Column("denominator", sa.Numeric(38, 12), nullable=True),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=True),
        sa.Column("metric_value_fingerprint", sa.CHAR(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("metric_value_id", name="pk_eval_metric_values"),
        sa.CheckConstraint(_METRIC_STATUS, name="ck_eval_metric_values_status"),
        sa.CheckConstraint("metric_version >= 1", name="ck_eval_metric_values_metric_version"),
        sa.CheckConstraint("sample_count >= 0", name="ck_eval_metric_values_sample_count"),
        sa.CheckConstraint(
            "denominator IS NULL OR denominator <> 0",
            name="ck_eval_metric_values_denominator_nonzero",
        ),
        sa.CheckConstraint(
            f"metric_value_fingerprint {_SHA256_CHECK}",
            name="ck_eval_metric_values_metric_value_fingerprint",
        ),
        sa.CheckConstraint(
            "value IS NULL OR value >= 0",
            name="ck_eval_metric_values_value_nonnegative",
        ),
        sa.UniqueConstraint(
            "metric_value_fingerprint",
            name="uq_eval_metric_values_metric_value_fingerprint",
        ),
    )
    op.create_index("ix_eval_metric_values_score_run_id", _VALUES, ["score_run_id"])

    op.create_table(
        _BINDINGS,
        sa.Column("binding_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "score_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("eval_score_runs.score_run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("label_fingerprint", sa.CHAR(64), nullable=False),
        sa.Column("label_schema_version", sa.Integer(), nullable=False),
        sa.Column("case_id", sa.String(128), nullable=False),
        sa.Column("case_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("binding_id", name="pk_eval_human_label_bindings"),
        sa.CheckConstraint(
            "label_schema_version >= 1",
            name="ck_eval_label_bindings_schema_version",
        ),
        sa.CheckConstraint(
            f"label_fingerprint {_SHA256_CHECK}",
            name="ck_eval_label_bindings_label_fingerprint",
        ),
        sa.UniqueConstraint(
            "label_fingerprint",
            name="uq_eval_label_bindings_label_fingerprint",
        ),
    )
    op.create_index("ix_eval_label_bindings_score_run_id", _BINDINGS, ["score_run_id"])

    op.create_table(
        _JUDGE_RUNS,
        sa.Column("judge_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "score_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("eval_score_runs.score_run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("judge_name", sa.String(64), nullable=False),
        sa.Column("judge_version", sa.Integer(), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("judge_config_fingerprint", sa.CHAR(64), nullable=False),
        sa.Column("judge_run_fingerprint", sa.CHAR(64), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("total_tokens", sa.BigInteger(), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("judge_output_fingerprint", sa.CHAR(64), nullable=True),
        sa.Column("judge_input_payload", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("judge_run_id", name="pk_eval_judge_runs"),
        sa.CheckConstraint(_RUN_STATUS, name="ck_eval_judge_runs_status"),
        sa.CheckConstraint("judge_version >= 1", name="ck_eval_judge_runs_judge_version"),
        sa.CheckConstraint(
            f"judge_config_fingerprint {_SHA256_CHECK}",
            name="ck_eval_judge_runs_judge_config_fingerprint",
        ),
        sa.CheckConstraint(
            f"judge_run_fingerprint {_SHA256_CHECK}",
            name="ck_eval_judge_runs_judge_run_fingerprint",
        ),
        sa.CheckConstraint(
            f"judge_output_fingerprint IS NULL OR judge_output_fingerprint {_SHA256_CHECK}",
            name="ck_eval_judge_runs_judge_output_fingerprint",
        ),
        sa.CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0",
            name="ck_eval_judge_runs_input_tokens",
        ),
        sa.CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0",
            name="ck_eval_judge_runs_output_tokens",
        ),
        sa.CheckConstraint(
            "total_tokens IS NULL OR total_tokens >= 0",
            name="ck_eval_judge_runs_total_tokens",
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_eval_judge_runs_duration_ms",
        ),
        sa.UniqueConstraint(
            "judge_run_fingerprint",
            name="uq_eval_judge_runs_judge_run_fingerprint",
        ),
    )
    op.create_index("ix_eval_judge_runs_score_run_id", _JUDGE_RUNS, ["score_run_id"])

    op.create_table(
        _JUDGE_RESULTS,
        sa.Column("judge_metric_result_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "judge_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("eval_judge_runs.judge_run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("metric_name", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("score", sa.Numeric(38, 12), nullable=True),
        sa.Column("rationale_ref", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("judge_metric_result_id", name="pk_eval_judge_metric_results"),
        sa.CheckConstraint(_METRIC_STATUS, name="ck_eval_judge_metric_results_status"),
        sa.CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 1)",
            name="ck_eval_judge_metric_results_score_range",
        ),
        sa.UniqueConstraint(
            "judge_run_id",
            "metric_name",
            name="uq_eval_judge_metric_results_run_metric",
        ),
    )
    op.create_index("ix_eval_judge_metric_results_judge_run_id", _JUDGE_RESULTS, ["judge_run_id"])


def downgrade() -> None:
    # 数据安全：评分历史是正式 immutable eval artifact（ScoringSpec → ScoreRun →
    # MetricValue / HumanLabelBinding / JudgeRun → JudgeMetricResult 完整链路），
    # 不在 downgrade 时静默删除。六张表任一存在行 → 拒绝回滚（不删除数据 /
    # 不修改行），alembic_version 保持 0047。全部为空时才允许回到 0046。
    for table in _TABLES:
        if _table_has_row(table):
            raise RuntimeError(
                f"cannot downgrade migration 0047: rows present in {table}; "
                "refusing to drop evaluation scoring history "
                "(alembic_version stays 0047)"
            )
    for table in reversed(_TABLES):
        op.drop_table(table)
