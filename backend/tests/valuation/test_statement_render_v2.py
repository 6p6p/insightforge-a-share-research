"""Valuation statement v2 确定性渲染测试（4C final acceptance spec C）。

v2 renderer 用 `metric_codes`（真实 verified Comparisons 的 metric_code，**不是
模型输出**）区分 statement scope：single PE / PB / PS vs multi 综合。断言冻结
文本逐字精确；single metric + mixed 是稳定 policy error；statement 全确定性
（同输入同输出、输入顺序无关）。历史 v1 Claim 不修改（无 update API，v1 语句
不再被任何代码路径重新渲染）。
"""

import pytest

from app.valuation.claim_contracts import (
    ValuationClaimAssessment,
    render_valuation_claim_statement,
)
from app.valuation.claim_errors import ValuationClaimDraftError

# ---------------------------------------------------- 冻结文本（v2 精确逐字）

_PE_HIGH = "基于市盈率比较，公司当前估值水平高于所选可比公司整体水平。"
_PE_INLINE = "基于市盈率比较，公司当前估值水平与所选可比公司整体大致相当。"
_PE_LOW = "基于市盈率比较，公司当前估值水平低于所选可比公司整体水平。"
_PE_UNCERTAIN = "现有市盈率比较不足以形成明确的相对估值判断。"

_PB_HIGH = "基于市净率比较，公司当前估值水平高于所选可比公司整体水平。"
_PB_INLINE = "基于市净率比较，公司当前估值水平与所选可比公司整体大致相当。"
_PB_LOW = "基于市净率比较，公司当前估值水平低于所选可比公司整体水平。"
_PB_UNCERTAIN = "现有市净率比较不足以形成明确的相对估值判断。"

_PS_HIGH = "基于市销率比较，公司当前估值水平高于所选可比公司整体水平。"
_PS_INLINE = "基于市销率比较，公司当前估值水平与所选可比公司整体大致相当。"
_PS_LOW = "基于市销率比较，公司当前估值水平低于所选可比公司整体水平。"
_PS_UNCERTAIN = "现有市销率比较不足以形成明确的相对估值判断。"

_MULTI_HIGH = "基于所选估值指标综合比较，公司当前相对估值水平高于所选可比公司整体水平。"
_MULTI_INLINE = "基于所选估值指标综合比较，公司当前相对估值水平与所选可比公司整体大致相当。"
_MULTI_LOW = "基于所选估值指标综合比较，公司当前相对估值水平低于所选可比公司整体水平。"
_MULTI_MIXED = "不同估值指标对公司的相对估值判断存在分化。"
_MULTI_UNCERTAIN = "现有估值指标比较不足以形成明确的方向性判断。"

# ---------------------------------------------------------------- single metric


@pytest.mark.parametrize(
    ("assessment", "expected"),
    [
        (ValuationClaimAssessment.RELATIVE_HIGH, _PE_HIGH),
        (ValuationClaimAssessment.BROADLY_IN_LINE, _PE_INLINE),
        (ValuationClaimAssessment.RELATIVE_LOW, _PE_LOW),
        (ValuationClaimAssessment.UNCERTAIN, _PE_UNCERTAIN),
    ],
)
def test_single_pe_statement(assessment, expected) -> None:
    assert render_valuation_claim_statement(assessment, ("pe_ttm",)) == expected


@pytest.mark.parametrize(
    ("assessment", "expected"),
    [
        (ValuationClaimAssessment.RELATIVE_HIGH, _PB_HIGH),
        (ValuationClaimAssessment.BROADLY_IN_LINE, _PB_INLINE),
        (ValuationClaimAssessment.RELATIVE_LOW, _PB_LOW),
        (ValuationClaimAssessment.UNCERTAIN, _PB_UNCERTAIN),
    ],
)
def test_single_pb_statement(assessment, expected) -> None:
    assert render_valuation_claim_statement(assessment, ("pb_mrq",)) == expected


@pytest.mark.parametrize(
    ("assessment", "expected"),
    [
        (ValuationClaimAssessment.RELATIVE_HIGH, _PS_HIGH),
        (ValuationClaimAssessment.BROADLY_IN_LINE, _PS_INLINE),
        (ValuationClaimAssessment.RELATIVE_LOW, _PS_LOW),
        (ValuationClaimAssessment.UNCERTAIN, _PS_UNCERTAIN),
    ],
)
def test_single_ps_statement(assessment, expected) -> None:
    assert render_valuation_claim_statement(assessment, ("ps_ttm",)) == expected


def test_single_metric_mixed_is_stable_policy_error() -> None:
    # single metric 不可能合法 mixed（mixed policy 要求 support 正负方向都有）。
    with pytest.raises(ValuationClaimDraftError):
        render_valuation_claim_statement(ValuationClaimAssessment.MIXED, ("pe_ttm",))


# ---------------------------------------------------------------- multiple metrics


def test_multi_pe_pb_statement() -> None:
    assert (
        render_valuation_claim_statement(
            ValuationClaimAssessment.RELATIVE_HIGH, ("pe_ttm", "pb_mrq")
        )
        == _MULTI_HIGH
    )


def test_multi_pe_pb_ps_statement() -> None:
    assert (
        render_valuation_claim_statement(
            ValuationClaimAssessment.RELATIVE_LOW, ("pe_ttm", "pb_mrq", "ps_ttm")
        )
        == _MULTI_LOW
    )


def test_multi_mixed_statement() -> None:
    assert (
        render_valuation_claim_statement(ValuationClaimAssessment.MIXED, ("pe_ttm", "pb_mrq"))
        == _MULTI_MIXED
    )


def test_multi_uncertain_statement() -> None:
    assert (
        render_valuation_claim_statement(ValuationClaimAssessment.UNCERTAIN, ("pe_ttm", "ps_ttm"))
        == _MULTI_UNCERTAIN
    )


# ---------------------------------------------------------------- determinism


def test_statement_deterministic_same_input() -> None:
    a = render_valuation_claim_statement(
        ValuationClaimAssessment.RELATIVE_HIGH, ("pe_ttm", "pb_mrq", "ps_ttm")
    )
    b = render_valuation_claim_statement(
        ValuationClaimAssessment.RELATIVE_HIGH, ("pe_ttm", "pb_mrq", "ps_ttm")
    )
    assert a == b == _MULTI_HIGH


def test_statement_input_order_independent() -> None:
    a = render_valuation_claim_statement(
        ValuationClaimAssessment.BROADLY_IN_LINE, ("pe_ttm", "pb_mrq")
    )
    b = render_valuation_claim_statement(
        ValuationClaimAssessment.BROADLY_IN_LINE, ("pb_mrq", "pe_ttm")
    )
    assert a == b == _MULTI_INLINE


def test_statement_metric_codes_deduped() -> None:
    a = render_valuation_claim_statement(
        ValuationClaimAssessment.RELATIVE_HIGH, ("pe_ttm", "pe_ttm", "pb_mrq")
    )
    b = render_valuation_claim_statement(
        ValuationClaimAssessment.RELATIVE_HIGH, ("pe_ttm", "pb_mrq")
    )
    assert a == b == _MULTI_HIGH


# ---------------------------------------------------------------- invalid input


def test_unknown_metric_code_rejected() -> None:
    with pytest.raises(ValuationClaimDraftError):
        render_valuation_claim_statement(ValuationClaimAssessment.RELATIVE_HIGH, ("ev_ebitda",))


def test_empty_metric_codes_rejected() -> None:
    with pytest.raises(ValuationClaimDraftError):
        render_valuation_claim_statement(ValuationClaimAssessment.RELATIVE_HIGH, ())


def test_unknown_assessment_rejected() -> None:
    with pytest.raises(ValuationClaimDraftError):
        render_valuation_claim_statement("not_an_assessment", ("pe_ttm",))
