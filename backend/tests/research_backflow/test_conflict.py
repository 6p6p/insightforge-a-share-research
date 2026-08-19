"""P1.7/P1.8 冲突仲裁单元测试（无 DB、无网络）。

Covers：确定性规则（不同财年 / 年度 vs 单季 / 累计 vs 单期 / 单位换算假冲突）、
LLM 兜底触发与硬校验（非法 preferred / 非法 resolution 拒绝）、无 LLM 时直接
返回确定性结果——全部注入 fake（0 DB，0 external call）。
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.research_backflow.conflict import (
    ConflictAdjudicator,
    ConflictResolutionKind,
    NumericCandidate,
    adjudicate_deterministic,
)
from app.research_backflow.errors import (
    ConflictInvalidPreferredEvidenceId,
    ConflictMalformedOutput,
)


def _candidate(
    evidence_id: uuid.UUID | None = None,
    *,
    metric_code: str = "revenue",
    number: str = "100000000",
    raw_unit: str | None = "元",
    quote: str | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
    period_kind: str = "duration",
    scope: str = "consolidated",
    currency: str = "CNY",
    published_at: date | None = None,
) -> NumericCandidate:
    """构造测试候选（数字走字符串统一 Decimal 化）。"""
    return NumericCandidate(
        evidence_id=evidence_id or uuid.uuid4(),
        metric_code=metric_code,
        quote=quote or f"营业收入 {number}{raw_unit or ''}",
        number=Decimal(number),
        raw_unit=raw_unit,
        period_start=period_start,
        period_end=period_end,
        period_kind=period_kind,
        scope=scope,
        currency=currency,
        published_at=published_at,
    )


class FakeConflictModel:
    """可记录调用 + 可注入响应（含非法响应）的 fake LLM 仲裁模型。"""

    def __init__(self, response: dict | None = None) -> None:
        self.response = response if response is not None else {
            "resolution": "true_conflict",
            "preferred_evidence_id": None,
            "reason": "两条证据数值确实不同",
        }
        self.calls: list[tuple[list[dict], str | None]] = []

    async def adjudicate(self, candidates: list[dict], hint: str | None = None) -> dict:
        self.calls.append((candidates, hint))
        return dict(self.response)


# ------------------------------------------------------------------ deterministic


def test_different_fiscal_period_no_false_conflict():
    """不同财年的同期指标不得折叠为"重复提取"假冲突 -> 上年同期对比，优先更新期间。"""
    a_id, b_id = uuid.uuid4(), uuid.uuid4()
    a = _candidate(  # 2024 年度营业收入 12 亿元
        a_id,
        number="1200000000",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
    )
    b = _candidate(  # 2023 年度营业收入 10 亿元
        b_id,
        number="1000000000",
        period_start=date(2023, 1, 1),
        period_end=date(2023, 12, 31),
    )
    result = adjudicate_deterministic(a, b)
    assert result.kind == ConflictResolutionKind.PRIOR_YEAR_COMPARISON
    assert result.preferred_evidence_id == a.evidence_id  # 优先更新（2024）
    assert result.kind != ConflictResolutionKind.EXTRACTION_ERROR


def test_annual_vs_quarterly_no_false_conflict():
    """年度数 vs 单季数（不同 period_end）-> unresolved，不得折叠为假冲突。"""
    a = _candidate(
        uuid.uuid4(),
        number="1200000000",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
    )
    b = _candidate(
        uuid.uuid4(),
        number="300000000",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 3, 31),
    )
    result = adjudicate_deterministic(a, b)
    assert result.kind == ConflictResolutionKind.UNRESOLVED
    assert result.kind != ConflictResolutionKind.EXTRACTION_ERROR


def test_cumulative_vs_single_period_no_false_conflict():
    """同一 period_end 的累计数 vs 单期数（数值相同也不折叠为假冲突）。"""
    cumulative = _candidate(
        uuid.uuid4(),
        number="500000000",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 6, 30),
    )
    single_period = _candidate(
        uuid.uuid4(),
        number="500000000",
        period_start=date(2024, 4, 1),
        period_end=date(2024, 6, 30),
    )
    # 有目标期间（单期口径）-> 优先单期。
    with_target = adjudicate_deterministic(
        cumulative, single_period, preferred_target_period_end=date(2024, 6, 30)
    )
    assert with_target.kind == ConflictResolutionKind.CUMULATIVE_VS_SINGLE_PERIOD
    assert with_target.preferred_evidence_id == single_period.evidence_id
    # 无目标期间 -> 不臆断偏好。
    no_target = adjudicate_deterministic(cumulative, single_period)
    assert no_target.kind == ConflictResolutionKind.CUMULATIVE_VS_SINGLE_PERIOD
    assert no_target.preferred_evidence_id is None
    assert no_target.kind != ConflictResolutionKind.EXTRACTION_ERROR


def test_same_value_different_unit_is_false_conflict():
    """同一数值不同单位（亿元 vs 万元）-> 单位换算后相同 -> 重复提取假冲突。"""
    a_id = uuid.uuid4()
    a = _candidate(
        a_id,
        number="100",
        raw_unit="亿元",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
    )
    b = _candidate(
        uuid.uuid4(),
        number="1000000",
        raw_unit="万元",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
    )
    result = adjudicate_deterministic(a, b)
    assert result.kind == ConflictResolutionKind.EXTRACTION_ERROR
    assert result.preferred_evidence_id == a_id


def test_different_scope_prefers_consolidated():
    """口径不同且恰好一方为 consolidated -> different_scope，优先合并口径。"""
    parent = _candidate(
        uuid.uuid4(),
        number="100000000",
        scope="parent",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
    )
    consolidated = _candidate(
        uuid.uuid4(),
        number="100000000",
        scope="consolidated",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
    )
    result = adjudicate_deterministic(parent, consolidated)
    assert result.kind == ConflictResolutionKind.DIFFERENT_SCOPE
    assert result.preferred_evidence_id == consolidated.evidence_id


def test_deterministic_true_conflict_both_published_by_target():
    """其余条件相同、数值不同且双方均不晚于目标 -> 确定性 true_conflict。"""
    target = date(2024, 6, 30)
    a = _candidate(
        uuid.uuid4(),
        number="100000000",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 6, 30),
        published_at=date(2024, 4, 1),
    )
    b = _candidate(
        uuid.uuid4(),
        number="120000000",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 6, 30),
        published_at=date(2024, 5, 1),
    )
    result = adjudicate_deterministic(a, b, preferred_target_period_end=target)
    assert result.kind == ConflictResolutionKind.TRUE_CONFLICT
    assert result.preferred_evidence_id is None


def test_from_dict_defaults():
    """from_dict：缺省字段取默认值（duration / consolidated / CNY），类型归一化。"""
    cand = NumericCandidate.from_dict(
        {
            "evidence_id": str(uuid.uuid4()),
            "metric_code": "revenue",
            "quote": "营业收入 100 万元",
            "number": "100",
        }
    )
    assert cand.period_kind == "duration"
    assert cand.scope == "consolidated"
    assert cand.currency == "CNY"
    assert cand.raw_unit is None
    assert cand.period_start is None
    assert cand.period_end is None
    assert cand.published_at is None
    assert cand.number == Decimal("100")


# ------------------------------------------------------------------ LLM adjudication


@pytest.mark.asyncio
async def test_deterministic_unresolved_triggers_llm_adjudication():
    """确定性 unresolved -> 触发 LLM 仲裁；preferred 必须在 a/b 候选视图内。"""
    target = date(2024, 6, 30)
    a_id, b_id = uuid.uuid4(), uuid.uuid4()
    # b 晚于目标发布 -> 规则 6 不满足 -> 确定性 unresolved。
    a = _candidate(a_id, number="100000000", published_at=date(2024, 12, 31))
    b = _candidate(b_id, number="120000000", published_at=date(2025, 3, 1))
    deterministic = adjudicate_deterministic(a, b, preferred_target_period_end=target)
    assert deterministic.kind == ConflictResolutionKind.UNRESOLVED

    model = FakeConflictModel(
        {
            "resolution": "true_conflict",
            "preferred_evidence_id": str(a_id),
            "reason": "同期间不同数值且双方均不晚于目标 -> 真实冲突",
        }
    )
    adjudicator = ConflictAdjudicator(model=model)
    result = await adjudicator.adjudicate_conflict(
        a, b, preferred_target_period_end=target
    )
    assert result.kind == ConflictResolutionKind.TRUE_CONFLICT
    assert result.preferred_evidence_id == a_id
    # fake 模型收到两份 JSON-safe 候选视图，evidence_id 与 a/b 对齐。
    assert len(model.calls) == 1
    candidates, hint = model.calls[0]
    assert [cand["evidence_id"] for cand in candidates] == [str(a_id), str(b_id)]
    assert isinstance(candidates[0]["number"], str)
    assert hint == "preferred_target_period_end=2024-06-30"


@pytest.mark.asyncio
async def test_llm_invalid_preferred_evidence_id_rejected():
    """LLM 返回的 preferred_evidence_id 不属于 a/b -> ConflictInvalidPreferredEvidenceId。"""
    target = date(2024, 6, 30)
    a = _candidate(uuid.uuid4(), number="100000000", published_at=date(2024, 12, 31))
    b = _candidate(uuid.uuid4(), number="120000000", published_at=date(2025, 1, 1))
    model = FakeConflictModel(
        {
            "resolution": "true_conflict",
            "preferred_evidence_id": str(uuid.uuid4()),  # 既不是 a 也不是 b
            "reason": "任意非法 preferred",
        }
    )
    adjudicator = ConflictAdjudicator(model=model)
    with pytest.raises(ConflictInvalidPreferredEvidenceId) as exc:
        await adjudicator.adjudicate_conflict(a, b, preferred_target_period_end=target)
    assert exc.value.code == "conflict_invalid_preferred_evidence_id"


@pytest.mark.asyncio
async def test_llm_malformed_resolution_rejected():
    """LLM 返回不在 8 种分类内的 resolution -> ConflictMalformedOutput。"""
    target = date(2024, 6, 30)
    a = _candidate(uuid.uuid4(), number="100000000", published_at=date(2024, 12, 31))
    b = _candidate(uuid.uuid4(), number="120000000", published_at=date(2025, 1, 1))
    model = FakeConflictModel(
        {"resolution": "magic_answer", "reason": "不是合法分类"}
    )
    adjudicator = ConflictAdjudicator(model=model)
    with pytest.raises(ConflictMalformedOutput) as exc:
        await adjudicator.adjudicate_conflict(a, b, preferred_target_period_end=target)
    assert exc.value.code == "conflict_malformed_output"


@pytest.mark.asyncio
async def test_no_llm_returns_deterministic():
    """无 LLM（model=None）或 use_llm=False -> 直接返回确定性结果，不调用模型。"""
    target = date(2024, 6, 30)
    a = _candidate(uuid.uuid4(), number="100000000", published_at=date(2024, 12, 31))
    b = _candidate(uuid.uuid4(), number="120000000", published_at=date(2025, 1, 1))
    deterministic = adjudicate_deterministic(a, b, preferred_target_period_end=target)
    assert deterministic.kind == ConflictResolutionKind.UNRESOLVED

    adjudicator = ConflictAdjudicator(model=None)
    result = await adjudicator.adjudicate_conflict(
        a, b, preferred_target_period_end=target
    )
    assert result == deterministic
    assert result.kind == ConflictResolutionKind.UNRESOLVED
    assert result.preferred_evidence_id is None

    # use_llm=False：即使有模型也不调用。
    model = FakeConflictModel()
    adjudicator_with_model = ConflictAdjudicator(model=model)
    result_no_llm = await adjudicator_with_model.adjudicate_conflict(
        a, b, preferred_target_period_end=target, use_llm=False
    )
    assert result_no_llm == deterministic
    assert model.calls == []
