"""Financial calculation contracts unit tests (stage 4B.2B, spec F/G/N).

零 LLM / 零 Chroma / 零 DB：验证冻结版本、input roles mapping、draft 只允许
语义输入（不得提供 result_value / result_unit / formula / Evidence ID / period
metadata / fingerprint），以及 fingerprint 确定性（与 input 顺序无关、不含
calculation_id / created_at）。
"""

from decimal import Decimal
from uuid import uuid4

import pytest

from app.financial.calculations.contracts import (
    FINANCIAL_CALCULATION_SCHEMA_VERSION,
    FORMULA_VERSION,
    CalculationCode,
    CalculationResultUnit,
    FinancialCalculationDraft,
    InputRole,
    calculation_input_roles,
    calculation_result_unit,
    compute_calculation_fingerprint,
    expected_metric_code,
    supported_calculation_codes,
)
from app.financial.calculations.errors import (
    FinancialCalculationInputError,
    FinancialCalculationInputMismatch,
)
from app.financial.contracts import MetricCode


def test_frozen_versions() -> None:
    assert FINANCIAL_CALCULATION_SCHEMA_VERSION == 1
    assert FORMULA_VERSION == 1


def test_supported_codes_exactly_seven() -> None:
    assert {c.value for c in supported_calculation_codes()} == {
        "absolute_change_cny",
        "yoy_growth_rate",
        "qoq_growth_rate",
        "gross_margin",
        "operating_margin",
        "net_margin_parent",
        "debt_to_assets_ratio",
    }


@pytest.mark.parametrize(
    ("code", "roles"),
    [
        (CalculationCode.ABSOLUTE_CHANGE_CNY, (InputRole.CURRENT, InputRole.BASELINE)),
        (CalculationCode.YOY_GROWTH_RATE, (InputRole.CURRENT, InputRole.BASELINE)),
        (CalculationCode.QOQ_GROWTH_RATE, (InputRole.CURRENT, InputRole.BASELINE)),
        (
            CalculationCode.GROSS_MARGIN,
            (InputRole.REVENUE, InputRole.OPERATING_COST),
        ),
        (
            CalculationCode.OPERATING_MARGIN,
            (InputRole.REVENUE, InputRole.OPERATING_PROFIT),
        ),
        (
            CalculationCode.NET_MARGIN_PARENT,
            (InputRole.REVENUE, InputRole.NET_PROFIT_PARENT),
        ),
        (
            CalculationCode.DEBT_TO_ASSETS_RATIO,
            (InputRole.TOTAL_ASSETS, InputRole.TOTAL_LIABILITIES),
        ),
    ],
)
def test_input_roles_per_code(code, roles) -> None:
    assert calculation_input_roles(code) == roles


def test_expected_metric_code_fixed_roles() -> None:
    assert expected_metric_code(InputRole.REVENUE) == MetricCode.REVENUE
    assert expected_metric_code(InputRole.OPERATING_COST) == MetricCode.OPERATING_COST
    assert expected_metric_code(InputRole.OPERATING_PROFIT) == MetricCode.OPERATING_PROFIT
    assert expected_metric_code(InputRole.NET_PROFIT_PARENT) == MetricCode.NET_PROFIT_PARENT
    assert expected_metric_code(InputRole.TOTAL_ASSETS) == MetricCode.TOTAL_ASSETS
    assert expected_metric_code(InputRole.TOTAL_LIABILITIES) == MetricCode.TOTAL_LIABILITIES


def test_expected_metric_code_current_baseline_is_none() -> None:
    assert expected_metric_code(InputRole.CURRENT) is None
    assert expected_metric_code(InputRole.BASELINE) is None


def test_result_unit_cny_for_absolute_change() -> None:
    assert calculation_result_unit(CalculationCode.ABSOLUTE_CHANGE_CNY) == CalculationResultUnit.CNY


@pytest.mark.parametrize(
    "code",
    [
        CalculationCode.YOY_GROWTH_RATE,
        CalculationCode.QOQ_GROWTH_RATE,
        CalculationCode.GROSS_MARGIN,
        CalculationCode.OPERATING_MARGIN,
        CalculationCode.NET_MARGIN_PARENT,
        CalculationCode.DEBT_TO_ASSETS_RATIO,
    ],
)
def test_result_unit_ratio_for_others(code) -> None:
    assert calculation_result_unit(code) == CalculationResultUnit.RATIO


def _draft(code=CalculationCode.GROSS_MARGIN, **kwargs) -> FinancialCalculationDraft:
    defaults = {
        "company_id": uuid4(),
        "calculation_code": code,
        "input_observation_ids": {
            InputRole.REVENUE: uuid4(),
            InputRole.OPERATING_COST: uuid4(),
        },
    }
    defaults.update(kwargs)
    return FinancialCalculationDraft(**defaults)


def test_draft_accepts_semantic_input_only() -> None:
    draft = _draft()
    assert draft.company_id is not None
    assert draft.calculation_code == CalculationCode.GROSS_MARGIN
    assert set(draft.input_observation_ids) == {InputRole.REVENUE, InputRole.OPERATING_COST}


def test_draft_rejects_company_not_uuid() -> None:
    with pytest.raises(FinancialCalculationInputError):
        _draft(company_id="not-a-uuid")


def test_draft_rejects_bad_code() -> None:
    with pytest.raises(FinancialCalculationInputError):
        _draft(calculation_code="unknown")  # type: ignore[arg-type]


def test_draft_rejects_missing_role() -> None:
    with pytest.raises(FinancialCalculationInputError):
        _draft(input_observation_ids={InputRole.REVENUE: uuid4()})


def test_draft_rejects_extra_role() -> None:
    with pytest.raises(FinancialCalculationInputError):
        _draft(
            input_observation_ids={
                InputRole.REVENUE: uuid4(),
                InputRole.OPERATING_COST: uuid4(),
                InputRole.CURRENT: uuid4(),
            }
        )


def test_draft_rejects_non_uuid_observation_id() -> None:
    with pytest.raises(FinancialCalculationInputError):
        _draft(
            input_observation_ids={
                InputRole.REVENUE: "x",
                InputRole.OPERATING_COST: uuid4(),
            }
        )


def test_draft_rejects_bool_observation_id() -> None:
    with pytest.raises(FinancialCalculationInputError):
        _draft(
            input_observation_ids={InputRole.REVENUE: True, InputRole.OPERATING_COST: uuid4()}  # type: ignore[dict-item]
        )


def test_draft_rejects_non_dict_inputs() -> None:
    with pytest.raises(FinancialCalculationInputError):
        _draft(input_observation_ids=[uuid4()])  # type: ignore[arg-type]


def test_draft_rejects_duplicate_observation_across_roles() -> None:
    """Gate 0 C：同一 Observation 充当 current + baseline → InputMismatch。"""
    obs_id = uuid4()
    with pytest.raises(FinancialCalculationInputMismatch):
        FinancialCalculationDraft(
            company_id=uuid4(),
            calculation_code=CalculationCode.ABSOLUTE_CHANGE_CNY,
            input_observation_ids={
                InputRole.CURRENT: obs_id,
                InputRole.BASELINE: obs_id,
            },
        )


# ---------------------------------------------------------------- fingerprint


def test_fingerprint_deterministic_and_role_sorted() -> None:
    company = uuid4()
    a = ("current", uuid4(), "b" * 64)
    b = ("baseline", uuid4(), "c" * 64)
    fp1 = compute_calculation_fingerprint(
        calculation_schema_version=1,
        formula_version=1,
        company_id=company,
        calculation_code="yoy_growth_rate",
        inputs=[a, b],  # baseline 在 current 后
        result_value=Decimal("0.2"),
        result_unit="ratio",
    )
    fp2 = compute_calculation_fingerprint(
        calculation_schema_version=1,
        formula_version=1,
        company_id=company,
        calculation_code="yoy_growth_rate",
        inputs=[b, a],  # 顺序颠倒 → 同一指纹
        result_value=Decimal("0.2"),
        result_unit="ratio",
    )
    assert fp1 == fp2
    assert len(fp1) == 64


def test_fingerprint_changes_with_result() -> None:
    company = uuid4()
    inputs = [("current", uuid4(), "b" * 64), ("baseline", uuid4(), "c" * 64)]

    def fp(value: Decimal) -> str:
        return compute_calculation_fingerprint(
            calculation_schema_version=1,
            formula_version=1,
            company_id=company,
            calculation_code="yoy_growth_rate",
            inputs=inputs,
            result_value=value,
            result_unit="ratio",
        )

    assert fp(Decimal("0.2")) != fp(Decimal("0.3"))


def test_fingerprint_changes_with_observation_id() -> None:
    company = uuid4()
    obs_a = uuid4()
    obs_b = uuid4()
    base = {
        "calculation_schema_version": 1,
        "formula_version": 1,
        "company_id": company,
        "calculation_code": "yoy_growth_rate",
        "result_value": Decimal("0.2"),
        "result_unit": "ratio",
    }

    def fp(obs_id) -> str:
        return compute_calculation_fingerprint(
            **base,
            inputs=[("current", obs_id, "b" * 64), ("baseline", uuid4(), "c" * 64)],
        )

    assert fp(obs_a) != fp(obs_b)


def test_fingerprint_changes_with_observation_fingerprint() -> None:
    company = uuid4()
    base = {
        "calculation_schema_version": 1,
        "formula_version": 1,
        "company_id": company,
        "calculation_code": "yoy_growth_rate",
        "result_value": Decimal("0.2"),
        "result_unit": "ratio",
    }

    def fp(obs_fp: str) -> str:
        return compute_calculation_fingerprint(
            **base,
            inputs=[("current", uuid4(), obs_fp), ("baseline", uuid4(), "c" * 64)],
        )

    assert fp("d" * 64) != fp("e" * 64)


def test_fingerprint_changes_with_company_and_code() -> None:
    base = {
        "calculation_schema_version": 1,
        "formula_version": 1,
        "inputs": [("current", uuid4(), "b" * 64), ("baseline", uuid4(), "c" * 64)],
        "result_value": Decimal("0.2"),
        "result_unit": "ratio",
    }
    reference = compute_calculation_fingerprint(
        **base, company_id=uuid4(), calculation_code="yoy_growth_rate"
    )
    company_fp = compute_calculation_fingerprint(
        **base, company_id=uuid4(), calculation_code="yoy_growth_rate"
    )
    code_fp = compute_calculation_fingerprint(
        **base, company_id=uuid4(), calculation_code="qoq_growth_rate"
    )
    assert company_fp != reference  # 不同 company（两次 uuid4）→ 不同指纹
    assert code_fp != reference  # 不同 calculation_code → 不同指纹


def test_fingerprint_is_sha256_hex() -> None:
    fp = compute_calculation_fingerprint(
        calculation_schema_version=1,
        formula_version=1,
        company_id=uuid4(),
        calculation_code="gross_margin",
        inputs=[("revenue", uuid4(), "b" * 64), ("operating_cost", uuid4(), "c" * 64)],
        result_value=Decimal("0.4"),
        result_unit="ratio",
    )
    assert len(fp) == 64
    assert all(c in "0123456789abcdef" for c in fp)
