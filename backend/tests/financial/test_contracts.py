"""Financial metric observation contracts unit tests (stage 4B.2A, spec F/G/H/I/M).

零 LLM / 零 Chroma / 零 DB：只验证 MetricCode 冻结 taxonomy、statement family
确定性 mapping、period 规则、FinancialMetricDraft 构造校验、canonical
fingerprint 确定性与敏感性。
"""

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from app.financial.contracts import (
    FINANCIAL_METRIC_SCHEMA_VERSION,
    FinancialMetricDraft,
    MetricCode,
    PeriodKind,
    RawUnit,
    StatementFamily,
    StatementScope,
    compute_metric_fingerprint,
    expected_period_kind,
    statement_family,
    supported_metric_codes,
)
from app.financial.errors import FinancialMetricInputError

_EXPECTED_METRIC_CODES = (
    "revenue",
    "operating_cost",
    "operating_profit",
    "profit_before_tax",
    "net_profit",
    "net_profit_parent",
    "net_profit_parent_excl_nonrecurring",
    "operating_cash_flow_net",
    "total_assets",
    "total_liabilities",
    "equity_parent",
)

_INCOME = (
    MetricCode.REVENUE,
    MetricCode.OPERATING_COST,
    MetricCode.OPERATING_PROFIT,
    MetricCode.PROFIT_BEFORE_TAX,
    MetricCode.NET_PROFIT,
    MetricCode.NET_PROFIT_PARENT,
    MetricCode.NET_PROFIT_PARENT_EXCL_NONRECURRING,
)
_CASH_FLOW = (MetricCode.OPERATING_CASH_FLOW_NET,)
_BALANCE = (
    MetricCode.TOTAL_ASSETS,
    MetricCode.TOTAL_LIABILITIES,
    MetricCode.EQUITY_PARENT,
)


def _draft(**overrides) -> FinancialMetricDraft:
    values = dict(
        company_id=uuid4(),
        source_evidence_card_id=uuid4(),
        metric_code=MetricCode.REVENUE,
        statement_scope=StatementScope.CONSOLIDATED,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        source_value_text="123,456",
        raw_unit=RawUnit.TEN_THOUSAND_YUAN,
    )
    values.update(overrides)
    return FinancialMetricDraft(**values)


_FIXED_COMPANY = uuid4()
_FIXED_EVIDENCE = uuid4()


def _fp(**overrides) -> str:
    values = dict(
        metric_schema_version=FINANCIAL_METRIC_SCHEMA_VERSION,
        company_id=_FIXED_COMPANY,
        source_evidence_card_id=_FIXED_EVIDENCE,
        metric_code="revenue",
        statement_scope="consolidated",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        period_kind="duration",
        source_value_text="123,456",
        raw_value=Decimal("123456"),
        raw_unit="ten_thousand_yuan",
        normalized_value_cny=Decimal("1234560000"),
    )
    values.update(overrides)
    return compute_metric_fingerprint(**values)


# ---------------------------------------------------------------- 冻结 / taxonomy


def test_metric_schema_version_frozen() -> None:
    assert FINANCIAL_METRIC_SCHEMA_VERSION == 1


def test_metric_code_taxonomy_exactly_11() -> None:
    codes = [c.value for c in MetricCode]
    assert codes == list(_EXPECTED_METRIC_CODES)
    assert len(codes) == 11


def test_statement_scope_values() -> None:
    assert [s.value for s in StatementScope] == ["consolidated", "parent"]


def test_period_kind_values() -> None:
    assert [p.value for p in PeriodKind] == ["duration", "instant"]


def test_raw_unit_values() -> None:
    assert [u.value for u in RawUnit] == [
        "yuan",
        "thousand_yuan",
        "ten_thousand_yuan",
        "hundred_million_yuan",
    ]


def test_supported_metric_codes_frozen_order() -> None:
    assert supported_metric_codes() == tuple(MetricCode)


# ---------------------------------------------------------------- family mapping


def test_statement_family_income() -> None:
    for code in _INCOME:
        assert statement_family(code) == StatementFamily.INCOME_STATEMENT


def test_statement_family_cash_flow() -> None:
    for code in _CASH_FLOW:
        assert statement_family(code) == StatementFamily.CASH_FLOW


def test_statement_family_balance() -> None:
    for code in _BALANCE:
        assert statement_family(code) == StatementFamily.BALANCE_SHEET


def test_statement_family_rejects_non_enum() -> None:
    with pytest.raises(FinancialMetricInputError):
        statement_family("revenue")


def test_expected_period_kind_balance_is_instant() -> None:
    for code in _BALANCE:
        assert expected_period_kind(code) == PeriodKind.INSTANT


def test_expected_period_kind_income_cashflow_is_duration() -> None:
    for code in _INCOME + _CASH_FLOW:
        assert expected_period_kind(code) == PeriodKind.DURATION


# ---------------------------------------------------------------- draft 校验


def test_draft_valid() -> None:
    draft = _draft()
    assert draft.company_id
    assert draft.source_value_text == "123,456"


def test_draft_trims_source_value_text() -> None:
    draft = _draft(source_value_text="  123,456  ")
    assert draft.source_value_text == "123,456"


def test_draft_rejects_non_uuid_company_id() -> None:
    with pytest.raises(FinancialMetricInputError):
        _draft(company_id="not-a-uuid")


def test_draft_rejects_non_uuid_source_evidence_card_id() -> None:
    with pytest.raises(FinancialMetricInputError):
        _draft(source_evidence_card_id="not-a-uuid")


def test_draft_rejects_non_metric_code() -> None:
    with pytest.raises(FinancialMetricInputError):
        _draft(metric_code="revenue")


def test_draft_rejects_non_statement_scope() -> None:
    with pytest.raises(FinancialMetricInputError):
        _draft(statement_scope="consolidated")


def test_draft_rejects_period_start_after_period_end() -> None:
    with pytest.raises(FinancialMetricInputError):
        _draft(period_start=date(2025, 1, 1), period_end=date(2024, 12, 31))


def test_draft_rejects_blank_source_value_text() -> None:
    with pytest.raises(FinancialMetricInputError):
        _draft(source_value_text="   ")


def test_draft_rejects_non_raw_unit() -> None:
    with pytest.raises(FinancialMetricInputError):
        _draft(raw_unit="yuan")


def test_draft_accepts_balance_sheet_instant_without_period_start() -> None:
    draft = _draft(
        metric_code=MetricCode.TOTAL_ASSETS,
        period_start=None,
        period_end=date(2024, 12, 31),
    )
    assert draft.period_start is None


# ---------------------------------------------------------------- fingerprint


def test_fingerprint_is_sha256_hex() -> None:
    fp = _fp()
    assert len(fp) == 64
    assert all(c in "0123456789abcdef" for c in fp)


def test_fingerprint_deterministic() -> None:
    assert _fp() == _fp()
    fixed = uuid4()
    assert _fp(company_id=fixed) != _fp(company_id=uuid4())


def test_fingerprint_sensitive_to_every_derived_field() -> None:
    base = _fp()
    assert _fp(source_value_text="123,457") != base  # value 变化
    assert _fp(raw_unit="yuan") != base  # unit 变化
    assert _fp(period_end=date(2023, 12, 31)) != base  # period 变化
    assert _fp(period_start=None, period_kind="instant") != base  # period 结构变化
    assert _fp(metric_code="net_profit") != base  # metric code 变化
    assert _fp(source_evidence_card_id=uuid4()) != base  # source evidence 变化
    assert _fp(company_id=uuid4()) != base  # company 变化
    assert _fp(statement_scope="parent") != base  # scope 变化
    assert _fp(normalized_value_cny=Decimal("1")) != base  # normalized 变化


def test_fingerprint_decimal_serialization_uses_str() -> None:
    # Decimal 用 str() 序列化（保留 scale）：同一数值不同 scale 会得到不同
    # 指纹——这**不是 bug**：raw_value 永远来自 parser 的规范化 Decimal
    # （同 source 同 scale），指纹因此确定性稳定。
    base = _fp()
    assert _fp(raw_value=Decimal("123456.000000000000")) != base
    # 显式锁定 str() 语义：trailing zero 影响序列化。
    assert str(Decimal("123456")) == "123456"
    assert str(Decimal("123456.000000000000")) == "123456.000000000000"


def test_fingerprint_does_not_include_observation_id_or_created_at() -> None:
    # compute_metric_fingerprint 的签名没有 metric_observation_id / created_at：
    # 不可能参与指纹（同一输入永远同一指纹）。
    fp1 = _fp()
    fp2 = _fp()
    assert fp1 == fp2
