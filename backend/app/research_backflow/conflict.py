"""P1.7/P1.8 financial conflict adjudication: deterministic rules -> optional LLM.

确定性为主、LLM 兜底的两级仲裁（兜底**不得放宽任何确定性检查**）：

- P1.7 `adjudicate_deterministic`：按优先级规则 1-6 对两个财务候选输出
  `ConflictAdjudication`（8 种 `ConflictResolutionKind`）；规则未命中一律
  `unresolved`，绝不臆断"无冲突"；
- P1.8 `ConflictAdjudicator`：确定性先行；仅当结果落 `unresolved` /
  `true_conflict` 且注入 `ConflictAdjudicationModel` 且 `use_llm=True`
  时，才把 JSON-safe 候选视图交给 LLM，并对 LLM 输出做硬校验（resolution
  必须在 8 种分类内；preferred_evidence_id 必须是 a/b 之一；reason 截断 300）。

关键判定都不靠模型：单位换算成 CNY 后"数值相同 + 同期间 + 同口径"才是重复提取
（假冲突）；期间 / 口径 / 累计单期 / 发布时效全部确定性判定。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from app.research_backflow.errors import (
    ConflictInvalidPreferredEvidenceId,
    ConflictMalformedOutput,
)


class ConflictResolutionKind(StrEnum):
    """P1.8 冲突仲裁结果分类（8 种，冻结；deterministic 规则只产出子集）。"""

    DIFFERENT_PERIOD = "different_period"
    DIFFERENT_SCOPE = "different_scope"
    PRIOR_YEAR_COMPARISON = "prior_year_comparison"
    CUMULATIVE_VS_SINGLE_PERIOD = "cumulative_vs_single_period"
    RESTATED_VALUE = "restated_value"
    EXTRACTION_ERROR = "extraction_error"
    TRUE_CONFLICT = "true_conflict"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class NumericCandidate:
    """一条待仲裁的财务数值候选（从 Evidence 派生，quote 为真实原文引用）。"""

    evidence_id: UUID
    metric_code: str
    quote: str  # 真实原文引用（EvidenceCard.quote_text）
    number: Decimal
    raw_unit: str | None  # None = 无单位（比率/百分比）
    period_start: date | None
    period_end: date | None
    period_kind: str  # 'instant' | 'duration'
    scope: str  # 'consolidated' | 'parent' 等
    currency: str
    published_at: date | None

    @classmethod
    def from_dict(cls, data: dict) -> NumericCandidate:
        """从 dict（DB / API / LLM 输出）构造；缺省字段取安全默认值。

        默认 period_kind='duration'、scope='consolidated'、currency='CNY'
        （A 股财务主流形态）；日期 / UUID / Decimal 全部宽松归一化。
        """
        return cls(
            evidence_id=_coerce_uuid(data["evidence_id"]),
            metric_code=str(data["metric_code"]),
            quote=str(data["quote"]),
            number=_coerce_decimal(data["number"]),
            raw_unit=data.get("raw_unit") or None,
            period_start=_coerce_date(data.get("period_start")),
            period_end=_coerce_date(data.get("period_end")),
            period_kind=str(data.get("period_kind", "duration")),
            scope=str(data.get("scope", "consolidated")),
            currency=str(data.get("currency", "CNY")),
            published_at=_coerce_date(data.get("published_at")),
        )


@dataclass(frozen=True)
class ConflictAdjudication:
    """一次冲突仲裁的确定性输出（不可变）。"""

    kind: ConflictResolutionKind
    preferred_evidence_id: UUID | None
    reasoning: str


# ------------------------------------------------------------------ coercion helpers


def _coerce_uuid(value) -> UUID:
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _coerce_decimal(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _coerce_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


# ------------------------------------------------------------------ unit conversion


# 单位 -> CNY 换算系数。中文术语（亿元/万元/千元/元）与 contracts.RawUnit
# 英文枚举（hundred_million_yuan/...）双别名，与 recovery 模块术语对齐。
_UNIT_TO_CNY: dict[str, Decimal] = {
    "亿元": Decimal("1e8"),
    "hundred_million_yuan": Decimal("1e8"),
    "万元": Decimal("1e4"),
    "ten_thousand_yuan": Decimal("1e4"),
    "千元": Decimal("1e3"),
    "thousand_yuan": Decimal("1e3"),
    "元": Decimal("1"),
    "yuan": Decimal("1"),
}


def _normalize_to_cny(number: Decimal, raw_unit: str | None) -> Decimal:
    """按原始单位换算为 CNY（元）。无单位 / 未知单位 -> 原样返回（不臆造换算）。"""
    if raw_unit is None:
        return number
    factor = _UNIT_TO_CNY.get(raw_unit.strip())
    if factor is None:
        return number
    return number * factor


# ------------------------------------------------------------------ deterministic rules


def _same_period(a: NumericCandidate, b: NumericCandidate) -> bool:
    """期间三元组（kind / start / end）完全相同才视为同一期间。"""
    return (
        a.period_kind == b.period_kind
        and a.period_start == b.period_start
        and a.period_end == b.period_end
    )


def _single_consolidated(a: NumericCandidate, b: NumericCandidate) -> NumericCandidate | None:
    """口径不同且恰好一方为 consolidated -> 返回该方；否则 None。"""
    if a.scope == "consolidated" and b.scope != "consolidated":
        return a
    if b.scope == "consolidated" and a.scope != "consolidated":
        return b
    return None


def _minus_one_year(value: date) -> date:
    """value 的一年（同日）前；2/29 -> 2/28（确定性）。"""
    try:
        return value.replace(year=value.year - 1)
    except ValueError:
        return value.replace(year=value.year - 1, day=28)


def _split_cumulative_single(
    a: NumericCandidate, b: NumericCandidate
) -> tuple[NumericCandidate | None, NumericCandidate | None]:
    """同一 period_end 下拆累计 / 单期：期间起点更早的一方是累计（覆盖更长窗口）。

    累计数（如年初至某期末）起点早于单期数（如本季度初），period_end 相同；起点
    相同 / 任一缺失 / 任一非 duration -> 无法拆分（None, None），不臆断。
    """
    if a.period_end is None or b.period_end is None or a.period_end != b.period_end:
        return None, None
    if a.period_kind != "duration" or b.period_kind != "duration":
        return None, None
    if a.period_start is None or b.period_start is None or a.period_start == b.period_start:
        return None, None
    if a.period_start < b.period_start:
        return a, b
    return b, a


def _both_published_by(a: NumericCandidate, b: NumericCandidate, target: date | None) -> bool:
    """规则 6 时效门槛：双方 published_at 均非空且 <= 目标期间截止日。

    无目标期间 / 任一发布时间缺失 -> 不满足（不得臆断"双方均早于目标"）。
    """
    if target is None or a.published_at is None or b.published_at is None:
        return False
    return a.published_at <= target and b.published_at <= target


def adjudicate_deterministic(
    a: NumericCandidate,
    b: NumericCandidate,
    preferred_target_period_end: date | None = None,
) -> ConflictAdjudication:
    """确定性仲裁（P1.7）：规则 1-6 严格按优先级顺序执行。

    命中明确情形（假冲突 / 口径 / 同比 / 累计单期 / 真实冲突）直接返回；规则未
    命中一律 unresolved（交 LLM 或人工），**绝不放宽检查**。
    """
    # 规则 1：指标不同 -> 无法按同一指标确定性仲裁。
    if a.metric_code != b.metric_code:
        return ConflictAdjudication(
            kind=ConflictResolutionKind.UNRESOLVED,
            preferred_evidence_id=None,
            reasoning=(
                f"指标不一致（{a.metric_code} vs {b.metric_code}），"
                "无法按同一指标确定性仲裁"
            ),
        )
    # 规则 2：单位换算为 CNY 后数值相同 + 同一期间 + 同一口径 -> 重复提取（假冲突）。
    if (
        _normalize_to_cny(a.number, a.raw_unit) == _normalize_to_cny(b.number, b.raw_unit)
        and _same_period(a, b)
        and a.scope == b.scope
    ):
        return ConflictAdjudication(
            kind=ConflictResolutionKind.EXTRACTION_ERROR,
            preferred_evidence_id=a.evidence_id,
            reasoning="同一指标/期间/口径，单位换算后数值一致 -> 重复提取导致的假冲突",
        )
    # 规则 3：口径不同且恰好一方为 consolidated -> 优先合并口径。
    consolidated = _single_consolidated(a, b)
    if consolidated is not None:
        return ConflictAdjudication(
            kind=ConflictResolutionKind.DIFFERENT_SCOPE,
            preferred_evidence_id=consolidated.evidence_id,
            reasoning="口径不同且恰好一方为合并报表（consolidated），优先采纳合并口径",
        )
    # 规则 4：duration 且 period_end 不同、b 恰为 a 的去年同期 -> 上年同期对比，优先更新期间。
    if (
        a.period_kind == "duration"
        and b.period_kind == "duration"
        and a.period_end is not None
        and b.period_end is not None
        and a.period_end != b.period_end
        and b.period_end == _minus_one_year(a.period_end)
    ):
        return ConflictAdjudication(
            kind=ConflictResolutionKind.PRIOR_YEAR_COMPARISON,
            preferred_evidence_id=a.evidence_id,
            reasoning="b 的 period_end 恰为 a 的去年同期 -> 上年同期对比，优先采纳更新期间",
        )
    # 规则 5：period_end 相同但一方为累计数 -> 累计 vs 单期；有目标期间时优先单期。
    cumulative, single_period = _split_cumulative_single(a, b)
    if cumulative is not None and single_period is not None:
        preferred = single_period.evidence_id if preferred_target_period_end is not None else None
        return ConflictAdjudication(
            kind=ConflictResolutionKind.CUMULATIVE_VS_SINGLE_PERIOD,
            preferred_evidence_id=preferred,
            reasoning="同一 period_end 下累计数 vs 单期数，优先采纳单期数值",
        )
    # 规则 6：指标/期间/口径/单位/货币相同但数值不同，且双方均不晚于目标发布 -> 真实冲突。
    if (
        _same_period(a, b)
        and a.scope == b.scope
        and a.raw_unit == b.raw_unit
        and (a.currency or "").upper() == (b.currency or "").upper()
        and _normalize_to_cny(a.number, a.raw_unit) != _normalize_to_cny(b.number, b.raw_unit)
        and _both_published_by(a, b, preferred_target_period_end)
    ):
        return ConflictAdjudication(
            kind=ConflictResolutionKind.TRUE_CONFLICT,
            preferred_evidence_id=None,
            reasoning="同一指标/期间/口径/单位/货币但数值不同，且双方均不晚于目标发布 -> 真实冲突",
        )
    # 规则未命中：不臆断结论，交 LLM / 人工仲裁。
    return ConflictAdjudication(
        kind=ConflictResolutionKind.UNRESOLVED,
        preferred_evidence_id=None,
        reasoning="不满足任何确定性仲裁规则，交 LLM/人工仲裁",
    )


# ------------------------------------------------------------------ LLM adjudication


class ConflictAdjudicationModel(Protocol):
    """LLM 仲裁协议：接收 JSON-safe 候选视图 list，返回 dict（只评判，不出数值）。"""

    async def adjudicate(
        self, candidates: list[dict], hint: str | None = None
    ) -> dict: ...


def numeric_candidate_view(candidate: NumericCandidate) -> dict:
    """NumericCandidate 的 JSON-safe 视图（供 LLM 消费；日期/Decimal -> ISO/str）。

    字段固定为：evidence_id, metric_code, quote, number, raw_unit, period_start,
    period_end, period_kind, scope, currency, published_at。
    """
    return {
        "evidence_id": str(candidate.evidence_id),
        "metric_code": candidate.metric_code,
        "quote": candidate.quote,
        "number": str(candidate.number),
        "raw_unit": candidate.raw_unit,
        "period_start": (
            candidate.period_start.isoformat() if candidate.period_start is not None else None
        ),
        "period_end": (
            candidate.period_end.isoformat() if candidate.period_end is not None else None
        ),
        "period_kind": candidate.period_kind,
        "scope": candidate.scope,
        "currency": candidate.currency,
        "published_at": (
            candidate.published_at.isoformat() if candidate.published_at is not None else None
        ),
    }


class ConflictAdjudicator:
    """P1.8 冲突仲裁器：确定性优先，未决 / 真冲突再让 LLM 评判（硬校验 LLM 输出）。"""

    def __init__(
        self,
        sessionmaker=None,
        model: ConflictAdjudicationModel | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._model = model

    async def adjudicate_conflict(
        self,
        a: NumericCandidate,
        b: NumericCandidate,
        preferred_target_period_end: date | None = None,
        use_llm: bool = True,
    ) -> ConflictAdjudication:
        """先确定性仲裁；未决 / 真冲突且可用 LLM 时再交给模型，最后校验 LLM 输出。"""
        deterministic = adjudicate_deterministic(a, b, preferred_target_period_end)
        if deterministic.kind not in (
            ConflictResolutionKind.UNRESOLVED,
            ConflictResolutionKind.TRUE_CONFLICT,
        ):
            # 确定性已给出明确结论（假冲突 / 口径 / 同比 / 累计单期）-> 不再问 LLM。
            return deterministic
        if self._model is None or not use_llm:
            return deterministic
        hint = None
        if preferred_target_period_end is not None:
            hint = f"preferred_target_period_end={preferred_target_period_end.isoformat()}"
        response = await self._model.adjudicate(
            [numeric_candidate_view(a), numeric_candidate_view(b)], hint=hint
        )
        return self._validate_llm_response(response, a, b)

    def _validate_llm_response(
        self, response: dict, a: NumericCandidate, b: NumericCandidate
    ) -> ConflictAdjudication:
        """LLM 输出硬校验：response 必须是 dict；分类合法；preferred 是 a/b；reason <= 300。"""
        if not isinstance(response, dict):
            raise ConflictMalformedOutput("LLM 仲裁输出必须是 dict")
        try:
            kind = ConflictResolutionKind(str(response.get("resolution", "")))
        except ValueError:
            raw_resolution = response.get("resolution")
            raise ConflictMalformedOutput(
                f"LLM 仲裁 resolution 不在 8 种分类内: {raw_resolution!r}"
            ) from None
        preferred_id: UUID | None = None
        raw_preferred = response.get("preferred_evidence_id")
        if raw_preferred not in (None, "", "null"):
            try:
                preferred_id = UUID(str(raw_preferred))
            except (ValueError, AttributeError):
                raise ConflictInvalidPreferredEvidenceId(
                    f"LLM 仲裁 preferred_evidence_id 不是合法 UUID: {raw_preferred!r}"
                ) from None
            if preferred_id not in (a.evidence_id, b.evidence_id):
                raise ConflictInvalidPreferredEvidenceId(
                    "LLM 仲裁 preferred_evidence_id 必须是两个候选之一"
                )
        reason = str(response.get("reason") or "").strip()[:300]
        return ConflictAdjudication(
            kind=kind, preferred_evidence_id=preferred_id, reasoning=reason
        )
