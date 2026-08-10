"""SQLAlchemy models for deterministic financial calculations (stage 4B.2B).

`financial_calculations` 把**已登记 FinancialMetricObservation** 通过确定性公式
计算为派生财务事实（同比 / 环比 / margin / ratio），供后续（4B.2C）Financial
Analyst 消费。

- provenance：每个 calculation 通过 `financial_calculation_inputs` 的
  `metric_observation_id`（FK financial_metric_observations RESTRICT）回到
  Observation → EvidenceCard → Source，形成
  Calculation → Observation → EvidenceCard → Source 的证据链。
- company_id / metric_observation_id FK RESTRICT：上游存在期间本行不会被级联
  删除；inputs 的 calculation_id FK CASCADE：删除 calculation 时级联删除其 inputs。
- result_value 全程 Decimal（CALCULATION_SCALE=12、ROUND_HALF_EVEN）；
  result_unit 只有 cny / ratio（ratio 存 0.1234，不存 12.34）。
- calculation_fingerprint UNIQUE：同一完全相同输入 → replay 同一行；输入任一
  变化 → 新指纹 → 新行，旧行保留（无 update API）。
- **无调用方提供的 result_value / result_unit / formula / Evidence ID /
  source ID / period metadata / fingerprint**：全部由 FinancialCalculationService
  从 draft 确定性派生。
"""

import uuid
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"
_CALCULATION_CODE_CHECK = (
    "calculation_code IN ("
    "'absolute_change_cny','yoy_growth_rate','qoq_growth_rate',"
    "'gross_margin','operating_margin','net_margin_parent','debt_to_assets_ratio')"
)
_RESULT_UNIT_CHECK = "result_unit IN ('cny','ratio')"
_INPUT_ROLE_CHECK = (
    "input_role IN ("
    "'current','baseline','revenue','operating_cost','operating_profit',"
    "'net_profit_parent','total_assets','total_liabilities')"
)


class FinancialCalculationModel(Base):
    """一条确定性的派生财务计算结果。"""

    __tablename__ = "financial_calculations"
    __table_args__ = (
        CheckConstraint(
            _CALCULATION_CODE_CHECK,
            name="ck_financial_calculations_calculation_code",
        ),
        CheckConstraint(
            _RESULT_UNIT_CHECK,
            name="ck_financial_calculations_result_unit",
        ),
        CheckConstraint(
            f"calculation_fingerprint {_SHA256_CHECK}",
            name="ck_financial_calculations_calculation_fingerprint",
        ),
        CheckConstraint(
            "calculation_schema_version >= 1",
            name="ck_financial_calculations_calculation_schema_version",
        ),
        CheckConstraint(
            "formula_version >= 1",
            name="ck_financial_calculations_formula_version",
        ),
        CheckConstraint(
            "btrim(calculation_code) <> ''",
            name="ck_financial_calculations_calculation_code_not_blank",
        ),
        UniqueConstraint(
            "calculation_fingerprint",
            name="uq_financial_calculations_calculation_fingerprint",
        ),
        Index("ix_financial_calculations_company_id", "company_id"),
        Index("ix_financial_calculations_calculation_code", "calculation_code"),
    )

    calculation_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("companies.company_id", ondelete="RESTRICT"),
        nullable=False,
    )
    calculation_code: Mapped[str] = mapped_column(String(64), nullable=False)
    result_value: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    result_unit: Mapped[str] = mapped_column(String(16), nullable=False)
    calculation_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    formula_version: Mapped[int] = mapped_column(Integer, nullable=False)
    calculation_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class FinancialCalculationInputModel(Base):
    """calculation → metric_observation 的输入绑定（一条一个 input_role）。

    PK(calculation_id, input_role)：同一 calculation 每个 input_role 恰好一行；
    `metric_observation_id` 索引支撑按 observation 反查 calculation。
    """

    __tablename__ = "financial_calculation_inputs"
    __table_args__ = (
        PrimaryKeyConstraint(
            "calculation_id", "input_role", name="pk_financial_calculation_inputs"
        ),
        CheckConstraint(
            _INPUT_ROLE_CHECK,
            name="ck_financial_calculation_inputs_input_role",
        ),
        CheckConstraint(
            "btrim(input_role) <> ''",
            name="ck_financial_calculation_inputs_input_role_not_blank",
        ),
        # 真实 DB 已存在（migration 0021）：同一 calculation 对同一 observation
        # 只能绑定一种 input_role。metadata 必须真实描述 current DB，供
        # alembic check 不产生 remove_constraint drift。
        UniqueConstraint(
            "calculation_id",
            "metric_observation_id",
            name="uq_financial_calculation_inputs_calc_observation",
        ),
        Index(
            "ix_financial_calculation_inputs_metric_observation_id",
            "metric_observation_id",
        ),
    )

    calculation_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("financial_calculations.calculation_id", ondelete="CASCADE"),
        nullable=False,
    )
    input_role: Mapped[str] = mapped_column(String(32), nullable=False)
    metric_observation_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("financial_metric_observations.metric_observation_id", ondelete="RESTRICT"),
        nullable=False,
    )
