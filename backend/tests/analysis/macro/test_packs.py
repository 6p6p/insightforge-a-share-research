"""MacroDriver/CompanyEvidence pack + M/E ref resolution unit tests (stage 4C.1B)。

验证：
- build_macro_driver_pack：M1..Mn 按 str(evidence_card_id) 升序（确定性）、
  最小投影（不发送 UUID / fingerprint / source UUID / locator）、空包拒绝；
- build_company_evidence_pack：E1..En 同上，两池 namespace 严格分离；
- assert_macro_statement_has_no_numeric_literals：ASCII/full-width digits / % /
  中文数字 / 定量短语 / numeric-context 拒绝，"若利率持续上行…" 等定性句允许；
  不自动删数字 / 不改写；
- resolve_decision_refs：未知 M/E ref → UnknownRef、跨 relation 冲突 →
  RelationConflict、组内去重 + canonical 排序、relevant=false → []。
"""

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest

from app.analysis.macro.contracts import (
    MacroAnalysisDecision,
    MacroAnalysisReason,
    MacroClaimCandidate,
)
from app.analysis.macro.errors import (
    MacroAnalysisInputError,
    MacroAnalysisNumericLiteralForbidden,
    MacroAnalysisRelationConflict,
    MacroAnalysisUnknownRef,
)
from app.analysis.macro.packs import (
    CompanyEvidencePackSource,
    MacroDriverPackSource,
    assert_macro_statement_has_no_numeric_literals,
    build_company_evidence_pack,
    build_macro_driver_pack,
    resolve_decision_refs,
)
from app.claims.contracts import ClaimKind
from app.claims.macro_contracts import (
    MacroChannelType,
    MacroClaimConfidence,
    MacroClaimImportance,
    MacroEffectDirection,
    MacroImpactStatus,
    MacroTimeAlignment,
)


def _uuid(n: int) -> UUID:
    return UUID(f"{n:08d}-0000-0000-0000-000000000000")


def _availability() -> datetime:
    return datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


def _driver_source(card_id: UUID | None = None, **overrides) -> MacroDriverPackSource:
    values = dict(
        evidence_card_id=card_id or uuid4(),
        origin_type="macro_observation",
        evidence_statement="央行宣布上调政策利率。",
        evidence_type="event",
        provider_key="world_bank",
        authority_tier_snapshot=1,
        availability=_availability(),
        effective_period_summary="观测期 2024（yearly）",
        indicator_name="Population, total",
        series_identity="world_bank CHN yearly",
        observation_period="2024",
        value_summary="1410000000 人",
        indicator_unit="人",
    )
    values.update(overrides)
    return MacroDriverPackSource(**values)


def _company_source(card_id: UUID | None = None, **overrides) -> CompanyEvidencePackSource:
    values = dict(
        evidence_card_id=card_id or uuid4(),
        evidence_statement="公司披露部分借款采用浮动利率计息。",
        evidence_type="statement",
        provider_key="xinhuanet",
        authority_tier_snapshot=3,
        availability=_availability(),
        quote_text="公司部分借款按浮动利率计息。",
    )
    values.update(overrides)
    return CompanyEvidencePackSource(**values)


def _candidate(**overrides) -> MacroClaimCandidate:
    values = dict(
        statement="若利率持续上行，公司融资成本存在上升压力。",
        claim_kind=ClaimKind.RISK,
        confidence=MacroClaimConfidence.MEDIUM,
        importance=MacroClaimImportance.NORMAL,
        channel_type=MacroChannelType.FINANCING,
        effect_direction=MacroEffectDirection.HEADWIND,
        impact_status=MacroImpactStatus.PLAUSIBLE_IMPACT,
        time_alignment=MacroTimeAlignment.ALIGNED,
        macro_driver_refs=["M1"],
        company_exposure_refs=["E1"],
        observed_effect_refs=[],
        additional_support_evidence_refs=[],
        additional_contradict_evidence_refs=[],
        additional_context_evidence_refs=[],
    )
    values.update(overrides)
    return MacroClaimCandidate(**values)


def _decision(**overrides) -> MacroAnalysisDecision:
    values = dict(relevant=True, claims=[_candidate()], reason_code=None)
    values.update(overrides)
    return MacroAnalysisDecision(**values)


# ---------------------------------------------------------------- MacroDriver Pack


def test_build_macro_driver_pack_orders_canonically() -> None:
    a, b, c = _uuid(30), _uuid(10), _uuid(20)
    pack = build_macro_driver_pack([_driver_source(a), _driver_source(b), _driver_source(c)])
    # 按 str(uuid) 升序 → M1/M2/M3 对应 uuid 10/20/30。
    assert [item.macro_ref for item in pack.items] == ["M1", "M2", "M3"]
    assert pack.ref_to_card_id == {"M1": b, "M2": c, "M3": a}
    assert pack.card_id_to_ref[b] == "M1"


def test_build_macro_driver_pack_deterministic_for_same_set() -> None:
    sources = [
        _driver_source(_uuid(5), evidence_statement="a"),
        _driver_source(_uuid(1), evidence_statement="b"),
        _driver_source(_uuid(3), evidence_statement="c"),
    ]
    first = build_macro_driver_pack(sources)
    second = build_macro_driver_pack(list(reversed(sources)))
    assert [item.evidence_statement for item in first.items] == [
        item.evidence_statement for item in second.items
    ]


def test_build_macro_driver_pack_empty_rejected() -> None:
    with pytest.raises(MacroAnalysisInputError):
        build_macro_driver_pack([])


def test_macro_driver_pack_projects_minimal_fields_only() -> None:
    source = _driver_source()
    pack = build_macro_driver_pack([source])
    item = pack.items[0]
    assert item.macro_ref == "M1"
    assert item.origin_type == "macro_observation"
    assert item.indicator_name == "Population, total"
    assert item.availability_date == date(2026, 8, 8)
    assert "2024" in item.effective_period_summary
    # 不发送内部字段（series_identity 是 human-readable 摘要，不是 series UUID；
    # 序列/观测/快照/source 的真实 UUID 一律不投影）。
    text = str(item)
    for forbidden in (
        str(source.evidence_card_id),
        "fingerprint",
        "evidence_card_id",
        "snapshot_id",
        "observation_id",
        "source_id",
        "locator",
        "chroma",
    ):
        assert forbidden not in text


def test_build_macro_driver_pack_document_driver_projection() -> None:
    source = _driver_source(
        origin_type="document_chunk",
        document_type="news_article",
        quote_text="央行宣布上调政策利率。",
        published_at=datetime(2026, 8, 8, 9, 0, tzinfo=UTC),
        reporting_period_end=date(2026, 6, 30),
    )
    item = build_macro_driver_pack([source]).items[0]
    assert item.origin_type == "document_chunk"
    assert item.document_type == "news_article"
    assert item.quote_text == "央行宣布上调政策利率。"
    assert item.published_at is not None
    assert item.reporting_period_end == date(2026, 6, 30)


# ---------------------------------------------------------------- CompanyEvidence Pack


def test_build_company_evidence_pack_orders_canonically() -> None:
    a, b = _uuid(20), _uuid(10)
    pack = build_company_evidence_pack([_company_source(a), _company_source(b)])
    assert [item.evidence_ref for item in pack.items] == ["E1", "E2"]
    assert pack.ref_to_card_id == {"E1": b, "E2": a}


def test_build_company_evidence_pack_empty_rejected() -> None:
    with pytest.raises(MacroAnalysisInputError):
        build_company_evidence_pack([])


def test_company_evidence_pack_projects_minimal_fields_only() -> None:
    source = _company_source()
    pack = build_company_evidence_pack([source])
    item = pack.items[0]
    assert item.evidence_ref == "E1"
    assert item.authority_tier == 3
    text = str(item)
    for forbidden in (
        str(source.evidence_card_id),
        "fingerprint",
        "evidence_card_id",
        "source_id",
        "locator",
        "chroma",
    ):
        assert forbidden not in text


# ---------------------------------------------------------------- Numeric-literal guard


def test_numeric_guard_rejects_ascii_digits_and_percent() -> None:
    for statement in (
        "利率上调50个基点",
        "融资成本上升15%",
        "2026年需求改善",
        "加息2档",
    ):
        with pytest.raises(MacroAnalysisNumericLiteralForbidden):
            assert_macro_statement_has_no_numeric_literals(statement)


def test_numeric_guard_rejects_fullwidth_digits() -> None:
    with pytest.raises(MacroAnalysisNumericLiteralForbidden):
        assert_macro_statement_has_no_numeric_literals("利率上调２５基点")
    with pytest.raises(MacroAnalysisNumericLiteralForbidden):
        assert_macro_statement_has_no_numeric_literals("融资成本上升２０％")


def test_numeric_guard_accepts_qualitative_statement() -> None:
    for statement in (
        "若利率持续上行，公司融资成本存在上升压力。",
        "汇率波动影响海外收入。",
        "大宗价格回落缓解成本压力。",
    ):
        assert_macro_statement_has_no_numeric_literals(statement)  # 不抛错


def test_numeric_guard_rejects_chinese_numeric_characters() -> None:
    for statement in (
        "利率上调五十个基点",
        "政策利率下降两档",
        "二〇二六年需求改善",
        "融资成本上升一成",
        "需求增长一半",
        "利率上升百点",
    ):
        with pytest.raises(MacroAnalysisNumericLiteralForbidden):
            assert_macro_statement_has_no_numeric_literals(statement)


def test_numeric_guard_accepts_non_numeric_yi_and_dian() -> None:
    # "一/点"本身允许（"一定/进一步/观点"等非数量词）。
    for statement in (
        "经营保持一定增长",
        "盈利空间有望进一步打开",
        "公司管理层观点保持谨慎",
    ):
        assert_macro_statement_has_no_numeric_literals(statement)  # 不抛错


def test_numeric_guard_rejects_quantitative_phrases() -> None:
    for statement in (
        "加息百分之十",
        "需求增长两倍",
        "成本实现翻倍",
        "收入实现翻番",
        "过半业务受影响",
        "半数客户延迟",
        "成本上升一个基点",
        "利率上升一个百分点",
    ):
        with pytest.raises(MacroAnalysisNumericLiteralForbidden):
            assert_macro_statement_has_no_numeric_literals(statement)


def test_numeric_guard_rejects_numeric_context_periods() -> None:
    for statement in (
        "一季度需求改善",
        "一月份需求增加",
        "第一期项目完成",
        "第一年度经营改善",
        "一日发生变化",
        "一号项目",
    ):
        with pytest.raises(MacroAnalysisNumericLiteralForbidden):
            assert_macro_statement_has_no_numeric_literals(statement)


# ---------------------------------------------------------------- Ref resolution


def test_resolve_refs_maps_m_and_e_to_uuids() -> None:
    a, b = _uuid(10), _uuid(20)
    driver_pack = build_macro_driver_pack([_driver_source(a), _driver_source(b)])
    company_pack = build_company_evidence_pack([_company_source(_uuid(50))])
    decision = _decision(
        claims=[
            _candidate(
                macro_driver_refs=["M1", "M2"],
                company_exposure_refs=["E1"],
            )
        ]
    )
    resolved = resolve_decision_refs(decision, driver_pack, company_pack)
    assert len(resolved) == 1
    claim = resolved[0]
    assert claim.macro_driver_ids == (a, b)  # 按 str(uuid) 升序
    assert claim.company_exposure_ids == (company_pack.ref_to_card_id["E1"],)


def test_resolve_refs_sorts_within_group_canonically() -> None:
    a, b = _uuid(30), _uuid(10)
    driver_pack = build_macro_driver_pack([_driver_source(a), _driver_source(b)])
    company_pack = build_company_evidence_pack([_company_source(_uuid(50))])
    # schema 已拒绝组内重复；此处输入未排序（["M2","M1"]）→ resolver 组内 canonical 排序。
    decision = _decision(
        claims=[
            _candidate(
                macro_driver_refs=["M2", "M1"],
                company_exposure_refs=["E1"],
            )
        ]
    )
    resolved = resolve_decision_refs(decision, driver_pack, company_pack)
    assert resolved[0].macro_driver_ids == (b, a)


def test_resolve_refs_unknown_m_rejected() -> None:
    driver_pack = build_macro_driver_pack([_driver_source(_uuid(10))])
    company_pack = build_company_evidence_pack([_company_source(_uuid(50))])
    decision = _decision(claims=[_candidate(macro_driver_refs=["M99"])])
    with pytest.raises(MacroAnalysisUnknownRef):
        resolve_decision_refs(decision, driver_pack, company_pack)


def test_resolve_refs_unknown_e_rejected() -> None:
    driver_pack = build_macro_driver_pack([_driver_source(_uuid(10))])
    company_pack = build_company_evidence_pack([_company_source(_uuid(50))])
    decision = _decision(claims=[_candidate(company_exposure_refs=["E99"])])
    with pytest.raises(MacroAnalysisUnknownRef):
        resolve_decision_refs(decision, driver_pack, company_pack)


def test_resolve_refs_cross_relation_e_conflict_rejected() -> None:
    driver_pack = build_macro_driver_pack([_driver_source(_uuid(10))])
    company_pack = build_company_evidence_pack([_company_source(_uuid(50))])
    # 同一 E 同时出现在 company_exposure 与 additional_context → 跨 relation 冲突。
    decision = _decision(
        claims=[
            _candidate(
                company_exposure_refs=["E1"],
                additional_context_evidence_refs=["E1"],
            )
        ]
    )
    with pytest.raises(MacroAnalysisRelationConflict):
        resolve_decision_refs(decision, driver_pack, company_pack)


def test_resolve_refs_non_relevant_returns_empty() -> None:
    driver_pack = build_macro_driver_pack([_driver_source(_uuid(10))])
    company_pack = build_company_evidence_pack([_company_source(_uuid(50))])
    decision = MacroAnalysisDecision(
        relevant=False,
        claims=[],
        reason_code=MacroAnalysisReason.INSUFFICIENT_COMPANY_EVIDENCE,
    )
    assert resolve_decision_refs(decision, driver_pack, company_pack) == []
