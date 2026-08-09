"""FinancialCalculationService pure-derive unit tests (stage 4B.2B, spec H-K).

零 DB / 零 LLM / 零 Chroma：用内存构造的 FinancialMetricObservationModel 验证
Service 的纯函数派生——comparability（company / scope / metric_code）、period
规则（absolute / YoY / QoQ）、storage bounds（result_value 超 NUMERIC(38,12)
显式拒绝）与 fingerprint 生成。
"""

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.db.models.financial_metric_observation import FinancialMetricObservationModel
from app.financial.calculations.contracts import (
    CalculationCode,
    FinancialCalculationDraft,
    InputRole,
)
from app.financial.calculations.errors import (
    FinancialCalculationCompanyMismatch,
    FinancialCalculationGrowthBaseNotPositive,
    FinancialCalculationInputMismatch,
    FinancialCalculationPeriodMismatch,
    FinancialCalculationScopeMismatch,
    FinancialCalculationStorageRangeError,
    FinancialCalculationZeroDenominator,
)
from app.financial.calculations.service import FinancialCalculationService

_COMPANY = uuid4()

_service = FinancialCalculationService(sessionmaker=None)  # type: ignore[arg-type]


def _obs(
    *,
    metric_code: str = "revenue",
    scope: str = "consolidated",
    period_start: date | None = date(2024, 1, 1),
    period_end: date = date(2024, 12, 31),
    period_kind: str = "duration",
    normalized: str = "12000000000",
    company: UUID = _COMPANY,
) -> FinancialMetricObservationModel:
    return FinancialMetricObservationModel(
        metric_observation_id=uuid4(),
        company_id=company,
        source_evidence_card_id=uuid4(),
        metric_code=metric_code,
        statement_scope=scope,
        period_start=period_start,
        period_end=period_end,
        period_kind=period_kind,
        source_value_text="123,456",
        raw_value=Decimal("123456"),
        raw_unit="ten_thousand_yuan",
        normalized_value_cny=Decimal(normalized),
        metric_schema_version=1,
        metric_fingerprint="a" * 64,
    )


def _growth_obs(
    *,
    current_start: date = date(2024, 1, 1),
    current_end: date = date(2024, 12, 31),
    baseline_start: date = date(2023, 1, 1),
    baseline_end: date = date(2023, 12, 31),
    current_norm: str = "12000000000",
    baseline_norm: str = "10000000000",
    current_kind: str = "duration",
    baseline_kind: str = "duration",
    current_scope: str = "consolidated",
    baseline_scope: str = "consolidated",
    current_code: str = "revenue",
    baseline_code: str = "revenue",
) -> dict:
    return {
        InputRole.CURRENT: _obs(
            period_start=current_start,
            period_end=current_end,
            period_kind=current_kind,
            scope=current_scope,
            metric_code=current_code,
            normalized=current_norm,
        ),
        InputRole.BASELINE: _obs(
            period_start=baseline_start,
            period_end=baseline_end,
            period_kind=baseline_kind,
            scope=baseline_scope,
            metric_code=baseline_code,
            normalized=baseline_norm,
        ),
    }


def _draft(code: CalculationCode, obs: dict) -> FinancialCalculationDraft:
    return FinancialCalculationDraft(
        company_id=_COMPANY,
        calculation_code=code,
        input_observation_ids={role: o.metric_observation_id for role, o in obs.items()},
    )


# ---------------------------------------------------------------- absolute change


def test_absolute_change_valid() -> None:
    obs = _growth_obs()
    derived = _service._derive(_draft(CalculationCode.ABSOLUTE_CHANGE_CNY, obs), obs)
    assert derived.result_value == Decimal("2000000000")
    assert derived.result_unit == "cny"
    assert len(derived.calculation_fingerprint) == 64


def test_absolute_change_period_kind_mismatch_rejected() -> None:
    obs = _growth_obs(baseline_kind="instant", baseline_start=None, baseline_end=date(2023, 12, 31))
    with pytest.raises(FinancialCalculationPeriodMismatch):
        _service._derive(_draft(CalculationCode.ABSOLUTE_CHANGE_CNY, obs), obs)


def test_absolute_change_negative_result() -> None:
    obs = _growth_obs(current_norm="8000000000", baseline_norm="10000000000")
    derived = _service._derive(_draft(CalculationCode.ABSOLUTE_CHANGE_CNY, obs), obs)
    assert derived.result_value == Decimal("-2000000000")


# ---------------------------------------------------------------- YoY


def test_yoy_valid_annual() -> None:
    obs = _growth_obs()
    derived = _service._derive(_draft(CalculationCode.YOY_GROWTH_RATE, obs), obs)
    assert derived.result_value == Decimal("0.2")
    assert derived.result_unit == "ratio"


def test_yoy_wrong_year_rejected() -> None:
    obs = _growth_obs(baseline_end=date(2022, 12, 31), baseline_start=date(2022, 1, 1))
    with pytest.raises(FinancialCalculationPeriodMismatch):
        _service._derive(_draft(CalculationCode.YOY_GROWTH_RATE, obs), obs)


def test_yoy_wrong_month_day_rejected() -> None:
    # 月/日不对应（2024-12-31 vs 2023-06-30）。
    obs = _growth_obs(baseline_start=date(2023, 1, 1), baseline_end=date(2023, 6, 30))
    with pytest.raises(FinancialCalculationPeriodMismatch):
        _service._derive(_draft(CalculationCode.YOY_GROWTH_RATE, obs), obs)


def test_yoy_quarter_end_correspondence() -> None:
    # 季度 YoY：2024Q3 vs 2023Q3（月/日对应）。
    obs = _growth_obs(
        current_start=date(2024, 7, 1),
        current_end=date(2024, 9, 30),
        baseline_start=date(2023, 7, 1),
        baseline_end=date(2023, 9, 30),
    )
    derived = _service._derive(_draft(CalculationCode.YOY_GROWTH_RATE, obs), obs)
    assert derived.result_value == Decimal("0.2")


# ---------------------------------------------------------------- QoQ


def test_qoq_duration_valid_consecutive() -> None:
    obs = _growth_obs(
        current_start=date(2024, 7, 1),
        current_end=date(2024, 9, 30),
        baseline_start=date(2024, 4, 1),
        baseline_end=date(2024, 6, 30),
    )
    derived = _service._derive(_draft(CalculationCode.QOQ_GROWTH_RATE, obs), obs)
    assert derived.result_value == Decimal("0.2")


def test_qoq_duration_cross_year_consecutive() -> None:
    obs = _growth_obs(
        current_start=date(2024, 1, 1),
        current_end=date(2024, 3, 31),
        baseline_start=date(2023, 10, 1),
        baseline_end=date(2023, 12, 31),
    )
    derived = _service._derive(_draft(CalculationCode.QOQ_GROWTH_RATE, obs), obs)
    assert derived.result_value == Decimal("0.2")


def test_qoq_duration_rejects_non_single_quarter() -> None:
    # current 是全年（非单季度）→ 拒绝。
    obs = _growth_obs(
        baseline_start=date(2024, 4, 1),
        baseline_end=date(2024, 6, 30),
    )
    with pytest.raises(FinancialCalculationPeriodMismatch):
        _service._derive(_draft(CalculationCode.QOQ_GROWTH_RATE, obs), obs)


def test_qoq_duration_rejects_non_consecutive() -> None:
    # 跳过了 Q2 → 拒绝。
    obs = _growth_obs(
        current_start=date(2024, 7, 1),
        current_end=date(2024, 9, 30),
        baseline_start=date(2024, 1, 1),
        baseline_end=date(2024, 3, 31),
    )
    with pytest.raises(FinancialCalculationPeriodMismatch):
        _service._derive(_draft(CalculationCode.QOQ_GROWTH_RATE, obs), obs)


def test_qoq_instant_valid_consecutive() -> None:
    obs = _growth_obs(
        current_start=None,
        current_end=date(2024, 9, 30),
        current_kind="instant",
        baseline_start=None,
        baseline_end=date(2024, 6, 30),
        baseline_kind="instant",
    )
    derived = _service._derive(_draft(CalculationCode.QOQ_GROWTH_RATE, obs), obs)
    assert derived.result_value == Decimal("0.2")


def test_qoq_instant_rejects_non_quarter_end() -> None:
    obs = _growth_obs(
        current_start=None,
        current_end=date(2024, 8, 31),
        current_kind="instant",
        baseline_start=None,
        baseline_end=date(2024, 6, 30),
        baseline_kind="instant",
    )
    with pytest.raises(FinancialCalculationPeriodMismatch):
        _service._derive(_draft(CalculationCode.QOQ_GROWTH_RATE, obs), obs)


def test_qoq_instant_rejects_non_consecutive() -> None:
    obs = _growth_obs(
        current_start=None,
        current_end=date(2024, 9, 30),
        current_kind="instant",
        baseline_start=None,
        baseline_end=date(2024, 3, 31),
        baseline_kind="instant",
    )
    with pytest.raises(FinancialCalculationPeriodMismatch):
        _service._derive(_draft(CalculationCode.QOQ_GROWTH_RATE, obs), obs)


# ---------------------------------------------------------------- comparability


def test_company_mismatch_rejected() -> None:
    obs = _growth_obs()
    obs[InputRole.CURRENT] = _obs(company=uuid4())
    with pytest.raises(FinancialCalculationCompanyMismatch):
        _service._derive(_draft(CalculationCode.YOY_GROWTH_RATE, obs), obs)


def test_scope_mismatch_rejected() -> None:
    obs = _growth_obs(baseline_scope="parent")
    with pytest.raises(FinancialCalculationScopeMismatch):
        _service._derive(_draft(CalculationCode.YOY_GROWTH_RATE, obs), obs)


def test_growth_metric_code_mismatch_rejected() -> None:
    obs = _growth_obs(baseline_code="net_profit")
    with pytest.raises(FinancialCalculationInputMismatch):
        _service._derive(_draft(CalculationCode.YOY_GROWTH_RATE, obs), obs)


def test_fixed_role_metric_code_mismatch_rejected() -> None:
    obs = {
        InputRole.REVENUE: _obs(metric_code="revenue"),
        InputRole.OPERATING_COST: _obs(metric_code="operating_profit"),  # 期望 operating_cost
    }
    with pytest.raises(FinancialCalculationInputMismatch):
        _service._derive(_draft(CalculationCode.GROSS_MARGIN, obs), obs)


def test_gross_margin_valid() -> None:
    obs = {
        InputRole.REVENUE: _obs(normalized="10000000000"),
        InputRole.OPERATING_COST: _obs(metric_code="operating_cost", normalized="6000000000"),
    }
    derived = _service._derive(_draft(CalculationCode.GROSS_MARGIN, obs), obs)
    assert derived.result_value == Decimal("0.4")
    assert derived.result_unit == "ratio"


# ---------------------------------------------------------------- error paths


def test_growth_base_not_positive_rejected() -> None:
    obs = _growth_obs(baseline_norm="0")
    with pytest.raises(FinancialCalculationGrowthBaseNotPositive):
        _service._derive(_draft(CalculationCode.YOY_GROWTH_RATE, obs), obs)


def test_zero_denominator_rejected() -> None:
    obs = {
        InputRole.REVENUE: _obs(normalized="0"),
        InputRole.OPERATING_COST: _obs(metric_code="operating_cost", normalized="100"),
    }
    with pytest.raises(FinancialCalculationZeroDenominator):
        _service._derive(_draft(CalculationCode.GROSS_MARGIN, obs), obs)


def test_result_storage_range_rejected() -> None:
    # current - baseline 超出 NUMERIC(38,12)（abs >= 10^26）→ 显式拒绝。
    obs = _growth_obs(
        current_norm="99999999999999999999999999",
        baseline_norm="-99999999999999999999999999",
    )
    with pytest.raises(FinancialCalculationStorageRangeError):
        _service._derive(_draft(CalculationCode.ABSOLUTE_CHANGE_CNY, obs), obs)


def test_fingerprint_stable_and_deterministic() -> None:
    obs = _growth_obs()
    draft = _draft(CalculationCode.YOY_GROWTH_RATE, obs)
    d1 = _service._derive(draft, obs)
    d2 = _service._derive(draft, obs)
    assert d1.calculation_fingerprint == d2.calculation_fingerprint
