"""ValuationComparison Pack builder + V ref resolution (stage 4C.2B.2).

- **ValuationComparison Pack**：从已通过 integrity 校验的 comparison 投影构造
  最小模型输入（V1..Vn **按 metric_code 排序**：pe_ttm → pb_mrq → ps_ttm；只存在
  的 comparison 编号）。每个 V item 只含 valuation_ref / metric_code / target_value /
  peer_median / peer_min / peer_max / premium_discount_to_median /
  position_vs_median（程序确定性：premium>0→above、<0→below、==0→equal）/
  peer_count / metric_as_of / analysis_as_of / comparison_method / formula_version /
  deterministic_display_premium（代码生成，如 `+50.00%`）。**不发送** comparison
  UUID / observation UUID / Evidence UUID / fingerprint / RawArtifact / locator /
  Chroma metadata。
- **position_vs_median / display premium 一律由代码生成**，模型不得计算百分比。
- **V ref resolution**：V<number> → comparison_id；未知引用 →
  ValuationAnalysisUnknownRef；跨 relation 冲突 → ValuationAnalysisRelationConflict；
  **no-cherry-picking 覆盖**（relevant=true 时 support ∪ contradict ∪ context 必须
  恰好等于全部 input aliases）→ ValuationAnalysisComparisonOmitted。不做 fuzzy
  resolve、不自动猜 UUID。

所有 alias 全确定性：同 comparison 集合 → 相同 V1..Vn 映射，ref resolution 可复现。
"""

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from uuid import UUID

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
from app.valuation.claim_contracts import (
    ValuationClaimAssessment,
    ValuationClaimConfidence,
    ValuationClaimImportance,
)

# v1 冻结 metric_code 的确定性排序（pack alias 按此分配 V1..Vn）。
_VALUATION_METRIC_ORDER = {"pe_ttm": 0, "pb_mrq": 1, "ps_ttm": 2}

_DISPLAY_QUANTUM = Decimal("0.01")


def _decimal_str(value: Decimal) -> str:
    """Decimal → 规范化字符串（去尾随 0；避免科学计数法）。

    DB numeric(14,12) 读出的 Decimal 会带 12 位尾零（如 `Decimal("15.300000000000")`），
    直接 `str()` 会把尾零带进模型输入。本 helper 保证同一数值在不同来源（单测
    直接构造 vs DB 读出）渲染一致；不改变数值精度，仅去掉尾随零。
    """
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def position_vs_median(premium: Decimal) -> str:
    """确定性 position（程序算好，模型不得计算）：above / below / equal。"""
    if premium > 0:
        return "above"
    if premium < 0:
        return "below"
    return "equal"


def render_display_premium(premium: Decimal) -> str:
    """确定性 display premium（代码生成，模型不得计算百分比）。

    premium 为小数比值（如 0.5）→ 百分比两位小数（如 `+50.00%`）；负值带 `-`
    前缀；0 → `0.00%`。ROUND_HALF_EVEN 两位小数。
    """
    pct = (premium * Decimal(100)).quantize(_DISPLAY_QUANTUM, rounding=ROUND_HALF_EVEN)
    if pct == 0:
        return "0.00%"
    return f"{'+' if pct > 0 else '-'}{abs(pct):f}%"


@dataclass(frozen=True)
class ValuationComparisonPackItem:
    """单次 comparison 在 Pack 中的最小投影（模型输入）。

    **只含必要字段**：valuation_ref / metric_code / target_value / peer_median /
    peer_min / peer_max / premium_discount_to_median / position_vs_median /
    peer_count / metric_as_of / analysis_as_of / comparison_method /
    formula_version / deterministic_display_premium。
    **不发送**：comparison UUID / observation UUID / Evidence UUID / fingerprint /
    locator / raw / Chroma。
    """

    valuation_ref: str
    metric_code: str
    target_value: str
    peer_median: str
    peer_min: str
    peer_max: str
    premium_discount_to_median: str
    position_vs_median: str
    peer_count: int
    metric_as_of: date
    analysis_as_of: date
    comparison_method: str
    formula_version: int
    deterministic_display_premium: str


@dataclass(frozen=True)
class ValuationComparisonPack:
    """本次分析的确定性 comparison Pack（V1..Vn 局部 alias → comparison_id）。

    - items：按 metric_code（pe_ttm/pb_mrq/ps_ttm）升序编号 V1..Vn（确定性）；
    - ref_to_comparison_id：V ref → comparison_id（ref resolution 用）；
    - comparison_id_to_ref：comparison_id → V ref（调试 / 日志用）。
    """

    items: tuple[ValuationComparisonPackItem, ...]
    ref_to_comparison_id: dict[str, UUID]
    comparison_id_to_ref: dict[UUID, str]


@dataclass(frozen=True)
class ValuationComparisonPackSource:
    """一次 comparison 的最小来源投影（由 Service 从 VerifiedComparison 构造）。

    数值字段都是已通过 replay integrity 核实的 persisted 派生值（Decimal，无
    float）。单元测试直接构造。
    """

    comparison_id: UUID
    metric_code: str
    target_value: Decimal
    peer_median: Decimal
    peer_min: Decimal
    peer_max: Decimal
    premium_discount_to_median: Decimal
    peer_count: int
    metric_as_of: date
    analysis_as_of: date
    comparison_method: str
    formula_version: int


def build_valuation_comparison_pack(
    sources: list[ValuationComparisonPackSource],
) -> ValuationComparisonPack:
    """构造确定性 comparison Pack（V1..Vn 按 metric_code 升序编号）。

    - 空包 → ValuationAnalysisInputError（分析必须有 comparison）；
    - alias 编号稳定：同 comparison 集合 → 相同 V1..Vn 映射，ref resolution 可复现；
    - position / display premium 由代码确定性生成（模型不计算）。
    """
    if not sources:
        raise ValuationAnalysisInputError("valuation comparison pack 不能为空")

    def _metric_key(source: ValuationComparisonPackSource) -> tuple:
        return (_VALUATION_METRIC_ORDER.get(source.metric_code, 1 << 30), str(source.comparison_id))

    ordered = sorted(sources, key=_metric_key)
    items: list[ValuationComparisonPackItem] = []
    ref_to_comparison_id: dict[str, UUID] = {}
    comparison_id_to_ref: dict[UUID, str] = {}
    for index, source in enumerate(ordered, start=1):
        ref = f"V{index}"
        items.append(
            ValuationComparisonPackItem(
                valuation_ref=ref,
                metric_code=source.metric_code,
                target_value=_decimal_str(source.target_value),
                peer_median=_decimal_str(source.peer_median),
                peer_min=_decimal_str(source.peer_min),
                peer_max=_decimal_str(source.peer_max),
                premium_discount_to_median=_decimal_str(source.premium_discount_to_median),
                position_vs_median=position_vs_median(source.premium_discount_to_median),
                peer_count=source.peer_count,
                metric_as_of=source.metric_as_of,
                analysis_as_of=source.analysis_as_of,
                comparison_method=source.comparison_method,
                formula_version=source.formula_version,
                deterministic_display_premium=render_display_premium(
                    source.premium_discount_to_median
                ),
            )
        )
        ref_to_comparison_id[ref] = source.comparison_id
        comparison_id_to_ref[source.comparison_id] = ref
    return ValuationComparisonPack(
        items=tuple(items),
        ref_to_comparison_id=ref_to_comparison_id,
        comparison_id_to_ref=comparison_id_to_ref,
    )


@dataclass(frozen=True)
class ResolvedValuationDecision:
    """解析完成、可直接构造 ValuationClaimDraft 的决策（V ref → UUID 已 resolve）。"""

    relevant: bool
    assessment: ValuationClaimAssessment | None
    confidence: ValuationClaimConfidence | None
    importance: ValuationClaimImportance | None
    support_comparison_ids: tuple[UUID, ...]
    contradict_comparison_ids: tuple[UUID, ...]
    context_comparison_ids: tuple[UUID, ...]
    reason_code: ValuationAnalysisReason | None


def resolve_decision_refs(
    decision: ValuationAnalysisDecision,
    pack: ValuationComparisonPack,
) -> ResolvedValuationDecision:
    """把 decision 中全部 V refs 解析为 comparison_id 并校验全覆盖（0 写失败）。

    - relevant=false → 空决策（不 resolve）；
    - relevant=true：
      - 未知 ref（不在 pack）→ ValuationAnalysisUnknownRef；
      - 同一 V ref 跨 relation 重复 → ValuationAnalysisRelationConflict；
      - **no-cherry-picking**：support ∪ contradict ∪ context 必须**恰好等于**
        request 全部 comparison aliases（遗漏任一 input → ValuationAnalysisComparisonOmitted）。
    组内去重 + canonical 排序（与 ValuationClaimDraft normalization 一致）。
    """
    if not decision.relevant:
        return ResolvedValuationDecision(
            relevant=False,
            assessment=None,
            confidence=None,
            importance=None,
            support_comparison_ids=(),
            contradict_comparison_ids=(),
            context_comparison_ids=(),
            reason_code=decision.reason_code,
        )
    groups = {
        "supports": decision.support_comparison_refs,
        "contradicts": decision.contradict_comparison_refs,
        "context": decision.context_comparison_refs,
    }
    # 未知引用检查（ref 格式已在 schema 校验为 V<number>）。
    for ref in (ref for group in groups.values() for ref in group):
        if ref not in pack.ref_to_comparison_id:
            raise ValuationAnalysisUnknownRef(f"unknown comparison ref: {ref}")
    # 跨 relation 重复检查（同一 ref 出现在 ≥2 个 relation 组）。
    relation_by_ref: dict[str, str] = {}
    for relation, refs in groups.items():
        for ref in refs:
            if ref in relation_by_ref:
                raise ValuationAnalysisRelationConflict(
                    f"comparison ref in multiple relations: {ref}"
                )
            relation_by_ref[ref] = relation
    # no-cherry-picking 覆盖：每个 input alias 必须出现且只出现一次。
    referenced = set(relation_by_ref)
    input_aliases = set(pack.ref_to_comparison_id)
    if referenced != input_aliases:
        omitted = sorted(input_aliases - referenced)
        raise ValuationAnalysisComparisonOmitted(f"omitted input comparison refs: {omitted}")
    # 组内去重 + canonical 排序（与 ValuationClaimDraft normalization 一致）。
    support_ids = tuple(
        sorted(
            (pack.ref_to_comparison_id[ref] for ref in groups["supports"]),
            key=str,
        )
    )
    contradict_ids = tuple(
        sorted(
            (pack.ref_to_comparison_id[ref] for ref in groups["contradicts"]),
            key=str,
        )
    )
    context_ids = tuple(
        sorted(
            (pack.ref_to_comparison_id[ref] for ref in groups["context"]),
            key=str,
        )
    )
    return ResolvedValuationDecision(
        relevant=True,
        assessment=decision.assessment,
        confidence=decision.confidence,
        importance=decision.importance,
        support_comparison_ids=support_ids,
        contradict_comparison_ids=contradict_ids,
        context_comparison_ids=context_ids,
        reason_code=None,
    )
