"""Calculation/Evidence pack + C/E ref resolution unit tests (stage 4B.2C.2)。

验证：
- build_calculation_pack：C1..Cn 按 str(calculation_id) 升序（确定性）、display value、
  period summary、最小投影（不发送 UUID / fingerprint / observation UUID）、空包拒绝；
- build_evidence_pack_allowing_empty：空 → 空 EvidencePack；非空委托 4B.1；
- assert_statement_has_no_numeric_literals：ASCII/full-width digits / % / 中文数字 /
  定量短语拒绝，"营业收入保持增长态势。" 等定性句允许；不自动删数字 / 不改写；
- resolve_decision_refs：未知 C/E ref → UnknownRef、跨 relation 冲突 →
  RelationConflict、组内去重 + canonical 排序、relevant=false → []。
"""

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.analysis.claims.contracts import EvidencePack, EvidencePackItem
from app.analysis.claims.evidence_pack import EvidencePackSource
from app.analysis.financial.contracts import (
    FinancialAnalysisDecision,
    FinancialAnalysisReason,
    FinancialClaimCandidate,
)
from app.analysis.financial.errors import (
    FinancialAnalysisInputError,
    FinancialAnalysisNumericLiteralForbidden,
    FinancialAnalysisRelationConflict,
    FinancialAnalysisUnknownRef,
)
from app.analysis.financial.packs import (
    CalculationPackSource,
    InputSummarySource,
    assert_statement_has_no_numeric_literals,
    build_calculation_pack,
    build_evidence_pack_allowing_empty,
    resolve_decision_refs,
)
from app.claims.contracts import ClaimKind
from app.claims.financial_contracts import (
    FinancialClaimConfidence,
    FinancialClaimImportance,
)


def _uuid(n: int) -> UUID:
    return UUID(f"{n:08d}-0000-0000-0000-000000000000")


def _input(
    role: str = "current",
    metric_code: str = "revenue",
    start: date = date(2024, 1, 1),
    end: date = date(2024, 12, 31),
    normalized: str = "12000000000",
) -> InputSummarySource:
    return InputSummarySource(
        role=role,
        metric_code=metric_code,
        statement_scope="consolidated",
        period_start=start,
        period_end=end,
        period_kind="duration",
        normalized_value_cny=Decimal(normalized),
    )


def _source(
    calculation_id: UUID = None,
    code: str = "yoy_growth_rate",
    result_value: Decimal = Decimal("0.2"),
    result_unit: str = "ratio",
    inputs: tuple[InputSummarySource, ...] = None,
) -> CalculationPackSource:
    return CalculationPackSource(
        calculation_id=calculation_id or uuid4(),
        calculation_code=code,
        result_value=result_value,
        result_unit=result_unit,
        formula_version=1,
        inputs=inputs
        or (
            _input(),
            _input(
                role="baseline",
                start=date(2023, 1, 1),
                end=date(2023, 12, 31),
                normalized="10000000000",
            ),
        ),
    )


def _pack(*items: EvidencePackItem) -> EvidencePack:
    ref_to_card_id = {item.evidence_ref: _uuid(100 + index) for index, item in enumerate(items)}
    return EvidencePack(
        items=tuple(items),
        ref_to_card_id=ref_to_card_id,
        card_id_to_ref={card_id: ref for ref, card_id in ref_to_card_id.items()},
    )


def _ev_item(
    ref: str = "E1", statement: str = "管理层解释营收增长主要来自直销渠道拓展。"
) -> EvidencePackItem:
    return EvidencePackItem(
        evidence_ref=ref,
        evidence_statement=statement,
        evidence_type="event",
        origin_type="document_chunk",
        authority_tier=3,
        provider_key="xinhuanet",
        quote_text=None,
        source_published_at=None,
        reporting_period_end=None,
    )


def _candidate(**overrides) -> FinancialClaimCandidate:
    values = dict(
        statement="营业收入保持增长态势。",
        claim_kind=ClaimKind.INFERENCE,
        confidence=FinancialClaimConfidence.HIGH,
        importance=FinancialClaimImportance.NORMAL,
        support_calculation_refs=["C1"],
        contradict_calculation_refs=[],
        context_calculation_refs=[],
        additional_support_evidence_refs=[],
        additional_contradict_evidence_refs=[],
        additional_context_evidence_refs=[],
    )
    values.update(overrides)
    return FinancialClaimCandidate(**values)


# ---------------------------------------------------------------- Calculation Pack


def test_build_calculation_pack_orders_canonically() -> None:
    a, b, c = _uuid(30), _uuid(10), _uuid(20)
    pack = build_calculation_pack([_source(a), _source(b), _source(c)])
    # 按 str(uuid) 升序 → C1/C2/C3 对应 uuid 10/20/30。
    assert [item.calculation_ref for item in pack.items] == ["C1", "C2", "C3"]
    assert pack.ref_to_calc_id == {"C1": b, "C2": c, "C3": a}
    assert pack.calc_id_to_ref[b] == "C1"


def test_build_calculation_pack_deterministic_for_same_set() -> None:
    sources = [
        _source(_uuid(5), code="a"),
        _source(_uuid(1), code="b"),
        _source(_uuid(3), code="c"),
    ]
    first = build_calculation_pack(sources)
    second = build_calculation_pack(list(reversed(sources)))
    assert [item.calculation_code for item in first.items] == [
        item.calculation_code for item in second.items
    ]


def test_build_calculation_pack_empty_rejected() -> None:
    with pytest.raises(FinancialAnalysisInputError):
        build_calculation_pack([])


def test_display_value_ratio_and_cny() -> None:
    ratio = build_calculation_pack([_source(result_value=Decimal("0.2"))]).items[0]
    assert ratio.deterministic_display_value == "20.00%"
    cny = build_calculation_pack(
        [_source(code="absolute_change_cny", result_value=Decimal("2000000000"), result_unit="cny")]
    ).items[0]
    assert cny.deterministic_display_value == "2000000000 CNY"


def test_period_summary_single_period() -> None:
    item = build_calculation_pack(
        [
            _source(
                inputs=(
                    _input(role="current"),
                    _input(role="baseline"),
                )
            )
        ]
    ).items[0]
    assert "duration 2024-01-01~2024-12-31" in item.period_summary


def test_calculation_pack_projects_minimal_fields_only() -> None:
    source = _source()
    pack = build_calculation_pack([source])
    item = pack.items[0]
    assert item.result_value == "0.2"
    assert item.result_unit == "ratio"
    assert item.formula_version == 1
    assert item.statement_scope == "consolidated"
    # input 摘要：role / metric_code / normalized_value_cny / unit=CNY。
    input_item = item.inputs[0]
    assert input_item.role == "current"
    assert input_item.metric_code == "revenue"
    assert input_item.normalized_value_cny == "12000000000"
    assert input_item.unit == "CNY"
    # 不发送内部字段。
    text = str(item)
    for forbidden in (
        str(source.calculation_id),
        "fingerprint",
        "metric_observation_id",
        "evidence_card_id",
    ):
        assert forbidden not in text


def test_calculation_pack_statement_scope_derived_from_inputs() -> None:
    source = _source()
    assert source.statement_scope == "consolidated"
    assert build_calculation_pack([source]).items[0].statement_scope == "consolidated"


# ---------------------------------------------------------------- Evidence Pack


def test_build_evidence_pack_allowing_empty_returns_empty() -> None:
    pack = build_evidence_pack_allowing_empty([])
    assert pack.items == ()
    assert pack.ref_to_card_id == {}


def test_build_evidence_pack_allowing_empty_non_empty_delegates() -> None:
    source = EvidencePackSource(
        evidence_card_id=_uuid(200),
        evidence_statement="管理层解释营收增长主要来自直销渠道拓展。",
        evidence_type="event",
        origin_type="document_chunk",
        authority_tier_snapshot=3,
        provider_key="xinhuanet",
    )
    pack = build_evidence_pack_allowing_empty([source])
    assert len(pack.items) == 1
    assert pack.items[0].evidence_ref == "E1"
    assert pack.ref_to_card_id["E1"] == _uuid(200)


# ---------------------------------------------------------------- Numeric-literal guard


def test_numeric_guard_rejects_ascii_digits_and_percent() -> None:
    for statement in (
        "收入同比增长20%",
        "利润率为15.3%",
        "2025年营业收入增长",
        "公司拥有2家子公司",
    ):
        with pytest.raises(FinancialAnalysisNumericLiteralForbidden):
            assert_statement_has_no_numeric_literals(statement)


def test_numeric_guard_rejects_fullwidth_digits() -> None:
    with pytest.raises(FinancialAnalysisNumericLiteralForbidden):
        assert_statement_has_no_numeric_literals("净利润增长１５％")
    with pytest.raises(FinancialAnalysisNumericLiteralForbidden):
        assert_statement_has_no_numeric_literals("毛利率达到２０%")


def test_numeric_guard_accepts_qualitative_statement() -> None:
    for statement in (
        "营业收入保持增长态势。",
        "公司流动性状况较上年有所改善。",
    ):
        assert_statement_has_no_numeric_literals(statement)  # 不抛错


def test_numeric_guard_rejects_chinese_numeric_characters() -> None:
    # 中文数字字符（零〇二两三四五六七八九十百千万亿兆）→ 拒绝；
    # "一成"由定量短语捕获（"一"本身允许，见 accept 用例）。
    for statement in (
        "营业收入增长两成",
        "利润同比增长一成",
        "二〇二五年收入改善",
        "公司拥有两家子公司",
        "营收规模达百亿元",
        "净利率提升一个百分点",
    ):
        with pytest.raises(FinancialAnalysisNumericLiteralForbidden):
            assert_statement_has_no_numeric_literals(statement)


def test_numeric_guard_accepts_non_numeric_yi_and_dian() -> None:
    # "一/点"在非数量词中允许（spec D 明确允许"存在一定盈利空间"）：
    # 真正的量与数字仍由字符 / 定量短语捕获。
    for statement in (
        "公司经营保持一定增长",
        "该指标反映公司存在一定盈利空间",
        "管理层观点保持谨慎",
        "盈利空间有望进一步打开",
        "公司统一推进主业协同",
    ):
        assert_statement_has_no_numeric_literals(statement)  # 不抛错


def test_numeric_guard_rejects_quantitative_yi_words() -> None:
    # 依赖"一"表达量的短语（一成 / 一半 / 一点）→ 拒绝。
    for statement in (
        "利润增长一成",
        "收入占公司一半",
        "盈利略有一点改善",
    ):
        with pytest.raises(FinancialAnalysisNumericLiteralForbidden):
            assert_statement_has_no_numeric_literals(statement)


def test_numeric_guard_rejects_quantitative_phrases() -> None:
    # 定量短语（百分之 / 倍 / 翻倍 / 翻番 / 过半 / 半数）→ 拒绝。
    for statement in (
        "营业收入增长百分之二十",
        "盈利能力提升一倍",
        "利润实现翻倍",
        "利润实现翻番",
        "过半收入来自新产品",
        "半数门店实现盈利",
    ):
        with pytest.raises(FinancialAnalysisNumericLiteralForbidden):
            assert_statement_has_no_numeric_literals(statement)


def test_numeric_guard_rejects_fullwidth_digit_percent() -> None:
    with pytest.raises(FinancialAnalysisNumericLiteralForbidden):
        assert_statement_has_no_numeric_literals("营业收入增长２０％")


def test_numeric_guard_accepts_spec_qualitative_statements() -> None:
    # Final closeout 验收 accept 用例：纯定性、无任何数字形式。
    for statement in (
        "营业收入保持增长态势",
        "盈利能力有所改善",
        "资产负债结构保持稳定",
    ):
        assert_statement_has_no_numeric_literals(statement)  # 不抛错


def test_numeric_guard_rejects_numeric_context_periods() -> None:
    # numeric-context：第? + 一 + 季/月/年/期/日/号，此时"一"是量词而非非数量词
    # 语素 → 拒绝（Gate A required 用例）。单一语义 pattern 覆盖全部形式。
    for statement in (
        "第一季度收入改善",
        "一季度收入改善",
        "一月份需求增加",
        "第一期项目完成",
        "第一年度经营改善",
        "一日发生变化",
        "一号项目",
    ):
        with pytest.raises(FinancialAnalysisNumericLiteralForbidden):
            assert_statement_has_no_numeric_literals(statement)


def test_numeric_guard_keeps_accepting_gate_a_context_phrases() -> None:
    # Gate A 要求继续允许：非数量词中的"一"（一定/进一步/观点）。
    for statement in (
        "公司存在一定盈利空间",
        "盈利有望进一步改善",
        "管理层观点保持谨慎",
    ):
        assert_statement_has_no_numeric_literals(statement)  # 不抛错


# ---------------------------------------------------------------- Ref resolution


def test_resolve_refs_maps_c_and_e_to_uuids() -> None:
    a, b = _uuid(10), _uuid(20)
    calc_pack = build_calculation_pack([_source(a, code="yoy"), _source(b, code="margin")])
    ev_pack = _pack(_ev_item(ref="E1"))
    decision = FinancialAnalysisDecision(
        relevant=True,
        claims=[
            _candidate(
                support_calculation_refs=["C1", "C2"],
                additional_context_evidence_refs=["E1"],
            )
        ],
    )
    resolved = resolve_decision_refs(decision, calc_pack, ev_pack)
    assert len(resolved) == 1
    claim = resolved[0]
    assert claim.supports_calculations == (a, b)  # 按 str(uuid) 升序
    assert claim.additional_context == (ev_pack.ref_to_card_id["E1"],)


def test_resolve_refs_sorts_and_dedupes_within_group() -> None:
    a, b = _uuid(30), _uuid(10)
    calc_pack = build_calculation_pack([_source(a), _source(b)])
    ev_pack = build_evidence_pack_allowing_empty([])
    # schema 已拒绝组内重复；此处未排序（["C2","C1"]）→ resolver 组内 canonical 排序。
    decision = FinancialAnalysisDecision(
        relevant=True, claims=[_candidate(support_calculation_refs=["C2", "C1"])]
    )
    resolved = resolve_decision_refs(decision, calc_pack, ev_pack)
    assert resolved[0].supports_calculations == (b, a)


def test_resolve_refs_unknown_c_rejected() -> None:
    calc_pack = build_calculation_pack([_source(_uuid(10))])
    ev_pack = build_evidence_pack_allowing_empty([])
    decision = FinancialAnalysisDecision(
        relevant=True, claims=[_candidate(support_calculation_refs=["C99"])]
    )
    with pytest.raises(FinancialAnalysisUnknownRef):
        resolve_decision_refs(decision, calc_pack, ev_pack)


def test_resolve_refs_unknown_e_rejected() -> None:
    calc_pack = build_calculation_pack([_source(_uuid(10))])
    ev_pack = _pack(_ev_item(ref="E1"))
    decision = FinancialAnalysisDecision(
        relevant=True,
        claims=[_candidate(additional_support_evidence_refs=["E99"])],
    )
    with pytest.raises(FinancialAnalysisUnknownRef):
        resolve_decision_refs(decision, calc_pack, ev_pack)


def test_resolve_refs_cross_relation_c_conflict_rejected() -> None:
    calc_pack = build_calculation_pack([_source(_uuid(10))])
    ev_pack = build_evidence_pack_allowing_empty([])
    decision = FinancialAnalysisDecision(
        relevant=True,
        claims=[_candidate(support_calculation_refs=["C1"], contradict_calculation_refs=["C1"])],
    )
    with pytest.raises(FinancialAnalysisRelationConflict):
        resolve_decision_refs(decision, calc_pack, ev_pack)


def test_resolve_refs_cross_relation_e_conflict_rejected() -> None:
    calc_pack = build_calculation_pack([_source(_uuid(10))])
    ev_pack = _pack(_ev_item(ref="E1"))
    decision = FinancialAnalysisDecision(
        relevant=True,
        claims=[
            _candidate(
                additional_support_evidence_refs=["E1"],
                additional_context_evidence_refs=["E1"],
            )
        ],
    )
    with pytest.raises(FinancialAnalysisRelationConflict):
        resolve_decision_refs(decision, calc_pack, ev_pack)


def test_resolve_refs_non_relevant_returns_empty() -> None:
    calc_pack = build_calculation_pack([_source(_uuid(10))])
    ev_pack = build_evidence_pack_allowing_empty([])
    decision = FinancialAnalysisDecision(
        relevant=False, claims=[], reason_code=FinancialAnalysisReason.NOT_RELEVANT
    )
    assert resolve_decision_refs(decision, calc_pack, ev_pack) == []
