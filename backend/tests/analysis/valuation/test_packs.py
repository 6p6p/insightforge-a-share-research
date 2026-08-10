"""Valuation comparison pack + V ref resolution unit tests (stage 4C.2B.2)。

验证：
- position_vs_median / render_display_premium 确定性（模型不计算百分比）；
- build_valuation_comparison_pack：V1..Vn 按 metric_code（pe_ttm→pb_mrq→ps_ttm）
  排序、同集合确定性、空包拒绝、最小投影（不发送 comparison UUID / fingerprint /
  observation UUID / locator）；
- resolve_decision_refs：V ref → comparison_id、组内去重 + canonical 排序、
  未知 ref → UnknownRef、跨 relation → RelationConflict、遗漏 input comparison →
  ComparisonOmitted（no-cherry-picking）、relevant=false → 空决策（reason 保留）。
"""

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.analysis.valuation.contracts import (
    ValuationAnalysisDecision,
    ValuationAnalysisReason,
)
from app.analysis.valuation.errors import (
    ValuationAnalysisComparisonOmitted,
    ValuationAnalysisInputError,
    ValuationAnalysisRelationConflict,
    ValuationAnalysisUnknownRef,
)
from app.analysis.valuation.packs import (
    ValuationComparisonPackSource,
    _decimal_str,
    build_valuation_comparison_pack,
    position_vs_median,
    render_display_premium,
    resolve_decision_refs,
)
from app.valuation.claim_contracts import (
    ValuationClaimAssessment,
    ValuationClaimConfidence,
    ValuationClaimImportance,
)

_AS_OF = date(2026, 8, 10)
_METRIC_AS_OF = date(2026, 8, 7)


def _uuid(n: int) -> UUID:
    return UUID(f"{n:08d}-0000-0000-0000-000000000000")


def _source(
    comparison_id: UUID | None = None,
    metric_code: str = "pe_ttm",
    premium: str = "0.02",
    **overrides,
) -> ValuationComparisonPackSource:
    values = dict(
        comparison_id=comparison_id or uuid4(),
        metric_code=metric_code,
        target_value=Decimal("15.3"),
        peer_median=Decimal("15.0"),
        peer_min=Decimal("14.2"),
        peer_max=Decimal("16.0"),
        premium_discount_to_median=Decimal(premium),
        peer_count=3,
        metric_as_of=_METRIC_AS_OF,
        analysis_as_of=_AS_OF,
        comparison_method="peer_median",
        formula_version=1,
    )
    values.update(overrides)
    return ValuationComparisonPackSource(**values)


def _decision(**overrides) -> ValuationAnalysisDecision:
    values = dict(
        relevant=True,
        assessment=ValuationClaimAssessment.RELATIVE_HIGH,
        confidence=ValuationClaimConfidence.HIGH,
        importance=ValuationClaimImportance.NORMAL,
        support_comparison_refs=["V1"],
        contradict_comparison_refs=[],
        context_comparison_refs=[],
        reason_code=None,
    )
    values.update(overrides)
    return ValuationAnalysisDecision(**values)


# ---------------------------------------------------------------- 确定性 helpers


def test_decimal_str_normalizes_trailing_zeros() -> None:
    # DB numeric(14,12) 读出带 12 位尾零 → 渲染应去掉尾零、避免科学计数法。
    assert _decimal_str(Decimal("15.300000000000")) == "15.3"
    assert _decimal_str(Decimal("15.0")) == "15"
    assert _decimal_str(Decimal("1500")) == "1500"  # 不落入科学计数法
    assert _decimal_str(Decimal("-0.200000000000")) == "-0.2"
    assert _decimal_str(Decimal("0")) == "0"
    assert _decimal_str(Decimal("0.000000000001")) == "0.000000000001"  # 不丢有效位


def test_position_vs_median_deterministic() -> None:
    assert position_vs_median(Decimal("0.02")) == "above"
    assert position_vs_median(Decimal("-0.02")) == "below"
    assert position_vs_median(Decimal("0")) == "equal"


def test_render_display_premium_deterministic() -> None:
    assert render_display_premium(Decimal("0.5")) == "+50.00%"
    assert render_display_premium(Decimal("-0.25")) == "-25.00%"
    assert render_display_premium(Decimal("0")) == "0.00%"
    # -0.2/15.5 无限循环 → ROUND_HALF_EVEN 12 位小数比值 → -1.29%。
    assert render_display_premium(Decimal("-0.012903225806")) == "-1.29%"


# ---------------------------------------------------------------- Pack


def test_build_pack_orders_by_metric_code() -> None:
    sources = [
        _source(_uuid(1), metric_code="ps_ttm", premium="0.3"),
        _source(_uuid(2), metric_code="pe_ttm", premium="0.02"),
        _source(_uuid(3), metric_code="pb_mrq", premium="-0.1"),
    ]
    pack = build_valuation_comparison_pack(sources)
    assert [item.valuation_ref for item in pack.items] == ["V1", "V2", "V3"]
    assert [item.metric_code for item in pack.items] == ["pe_ttm", "pb_mrq", "ps_ttm"]
    assert pack.ref_to_comparison_id["V1"] == _uuid(2)
    assert pack.ref_to_comparison_id["V2"] == _uuid(3)
    assert pack.ref_to_comparison_id["V3"] == _uuid(1)
    assert pack.comparison_id_to_ref[_uuid(1)] == "V3"


def test_build_pack_deterministic_for_same_set() -> None:
    sources = [
        _source(_uuid(5), metric_code="pe_ttm"),
        _source(_uuid(1), metric_code="pb_mrq"),
        _source(_uuid(3), metric_code="ps_ttm"),
    ]
    first = build_valuation_comparison_pack(sources)
    second = build_valuation_comparison_pack(list(reversed(sources)))
    assert [item.valuation_ref for item in first.items] == [
        item.valuation_ref for item in second.items
    ]
    assert first.ref_to_comparison_id == second.ref_to_comparison_id


def test_build_pack_empty_rejected() -> None:
    with pytest.raises(ValuationAnalysisInputError):
        build_valuation_comparison_pack([])


def test_build_pack_projects_minimal_fields_only() -> None:
    source = _source(_uuid(10), premium="0.02")
    pack = build_valuation_comparison_pack([source])
    item = pack.items[0]
    assert item.valuation_ref == "V1"
    assert item.target_value == "15.3"
    # _decimal_str 规范化：尾随 0 去掉（DB numeric(14,12) 读出同数值渲染一致）。
    assert item.peer_median == "15"
    assert item.peer_min == "14.2"
    assert item.peer_max == "16"
    assert item.premium_discount_to_median == "0.02"
    assert item.position_vs_median == "above"
    assert item.peer_count == 3
    assert item.metric_as_of == _METRIC_AS_OF
    assert item.analysis_as_of == _AS_OF
    assert item.comparison_method == "peer_median"
    assert item.formula_version == 1
    assert item.deterministic_display_premium == "+2.00%"
    # 不发送内部字段。
    text = str(item)
    for forbidden in (
        str(source.comparison_id),
        "fingerprint",
        "valuation_observation_id",
        "evidence_card_id",
        "locator",
        "chroma",
    ):
        assert forbidden not in text


# ---------------------------------------------------------------- Ref resolution


def test_resolve_refs_maps_v_to_uuids() -> None:
    a, b = _uuid(10), _uuid(20)
    pack = build_valuation_comparison_pack(
        [_source(a, metric_code="pe_ttm"), _source(b, metric_code="pb_mrq")]
    )
    # V1=pe(uuid10), V2=pb(uuid20)；support 未排序（["V2","V1"]）→ resolver 组内 canonical。
    decision = _decision(support_comparison_refs=["V2", "V1"], context_comparison_refs=[])
    resolved = resolve_decision_refs(decision, pack)
    assert resolved.support_comparison_ids == (a, b)  # 按 str(uuid) 升序
    assert resolved.contradict_comparison_ids == ()
    assert resolved.context_comparison_ids == ()


def test_resolve_refs_preserves_relation_groups() -> None:
    a, b = _uuid(10), _uuid(20)
    pack = build_valuation_comparison_pack(
        [_source(a, metric_code="pe_ttm"), _source(b, metric_code="pb_mrq")]
    )
    decision = _decision(
        support_comparison_refs=["V1"],
        context_comparison_refs=["V2"],
    )
    resolved = resolve_decision_refs(decision, pack)
    assert resolved.support_comparison_ids == (a,)
    assert resolved.context_comparison_ids == (b,)


def test_resolve_refs_unknown_rejected() -> None:
    pack = build_valuation_comparison_pack([_source(_uuid(10))])
    with pytest.raises(ValuationAnalysisUnknownRef):
        resolve_decision_refs(_decision(support_comparison_refs=["V99"]), pack)


def test_resolve_refs_cross_relation_conflict_rejected() -> None:
    pack = build_valuation_comparison_pack([_source(_uuid(10))])
    with pytest.raises(ValuationAnalysisRelationConflict):
        resolve_decision_refs(
            _decision(support_comparison_refs=["V1"], contradict_comparison_refs=["V1"]), pack
        )


def test_resolve_refs_omitted_input_rejected() -> None:
    """no-cherry-picking：2 个 input comparison 只引用 1 个 → ComparisonOmitted（0 写）。"""
    a, b = _uuid(10), _uuid(20)
    pack = build_valuation_comparison_pack(
        [_source(a, metric_code="pe_ttm"), _source(b, metric_code="pb_mrq")]
    )
    with pytest.raises(ValuationAnalysisComparisonOmitted):
        resolve_decision_refs(_decision(support_comparison_refs=["V1"]), pack)


def test_resolve_refs_non_relevant_returns_empty_decision() -> None:
    pack = build_valuation_comparison_pack([_source(_uuid(10))])
    decision = ValuationAnalysisDecision(
        relevant=False,
        assessment=None,
        confidence=None,
        importance=None,
        support_comparison_refs=[],
        contradict_comparison_refs=[],
        context_comparison_refs=[],
        reason_code=ValuationAnalysisReason.NOT_RELEVANT,
    )
    resolved = resolve_decision_refs(decision, pack)
    assert resolved.relevant is False
    assert resolved.assessment is None
    assert resolved.support_comparison_ids == ()
    assert resolved.reason_code == ValuationAnalysisReason.NOT_RELEVANT
