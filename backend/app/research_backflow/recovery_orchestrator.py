"""P1.1/1.4/1.10 recovery orchestration: gap 分类 -> RecoveryPlan -> 分类结果。

接线层：复用 recovery.py 冻结确定性 core（classify_gap / recovery_exhausted）+
financial_recovery.py（真实 quote -> observation），把结果规约为 smoke 所需五类计数。

绝不：吞异常假装成功、删除/放宽既有审计校验、用 LLM 记忆或编造数字作证据。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.research_backflow.recovery import GapClass, classify_gap, recovery_exhausted


class RecoveryCategory(StrEnum):
    """恢复结果五类（smoke 前后对照计数用）。"""

    RECOVERED = "recovered"
    RESOLVED_CONFLICT = "resolved_conflict"
    WORDING_FIXED = "wording_fixed"
    TRUE_MISSING = "true_missing"
    UNRESOLVED = "unresolved"


class RecoveryStrategy(StrEnum):
    RETRY_RETRIEVAL = "retry_retrieval"
    RE_EXTRACT = "re_extract"
    CONFLICT_ADJUDICATE = "conflict_adjudicate"
    SUPPLEMENTARY_DISCOVERY = "supplementary_discovery"
    WORDING_REWRITE = "wording_rewrite"
    TRUE_MISSING = "true_missing"


_GAP_TO_STRATEGY: dict[GapClass, RecoveryStrategy] = {
    GapClass.RETRIEVAL_MISS: RecoveryStrategy.RETRY_RETRIEVAL,
    GapClass.EXTRACTION_MISS: RecoveryStrategy.RE_EXTRACT,
    GapClass.SOURCE_GAP: RecoveryStrategy.SUPPLEMENTARY_DISCOVERY,
    GapClass.CONFLICT: RecoveryStrategy.CONFLICT_ADJUDICATE,
}


@dataclass(frozen=True)
class RecoveryPlan:
    """对单个缺口的恢复决策（确定性 gap 分类 + 动作清单）。"""

    need_code: str
    strategy: RecoveryStrategy
    gap_class: GapClass | None = None
    actions: tuple[str, ...] = field(default_factory=tuple)
    requires_supplementary: bool = False
    requires_conflict_adjudication: bool = False
    requires_wording_rewrite: bool = False


def build_recovery_plan(
    need_code: str,
    *,
    has_source: bool,
    has_chunk: bool,
    has_evidence: bool = False,
    wording_overclaim: bool = False,
) -> RecoveryPlan:
    if wording_overclaim:
        # P1.9：证据充分但报告措辞超出证据 → 定向措辞重写（非来源发现/非编造）。
        return RecoveryPlan(
            need_code=need_code,
            strategy=RecoveryStrategy.WORDING_REWRITE,
            gap_class=GapClass.CONFLICT if False else None,
            actions=("对受影响段落做保守措辞重写后重审",),
            requires_wording_rewrite=True,
        )
    # classify_gap 在 has_evidence=True 时抛错（缺口已解决）；防御性短路。
    if has_evidence:
        return RecoveryPlan(
            need_code=need_code,
            strategy=RecoveryStrategy.TRUE_MISSING,
            gap_class=None,
            actions=("已有证据，无需恢复",),
        )
    cls = classify_gap(has_source=has_source, has_chunk=has_chunk, has_evidence=False)
    strategy = _GAP_TO_STRATEGY.get(cls, RecoveryStrategy.TRUE_MISSING)
    actions: list[str] = []
    if strategy == RecoveryStrategy.RETRY_RETRIEVAL:
        actions = ["重试检索（扩展 alias / 提高 top_k）"]
    elif strategy == RecoveryStrategy.RE_EXTRACT:
        actions = ["对已有 chunk 重新抽取证据"]
    elif strategy == RecoveryStrategy.SUPPLEMENTARY_DISCOVERY:
        actions = ["补充真实公开来源后重试"]
    elif strategy == RecoveryStrategy.CONFLICT_ADJUDICATE:
        actions = ["口径/期间冲突裁定，收敛到单一候选证据"]
    else:
        strategy = RecoveryStrategy.TRUE_MISSING
        actions = ["现有公开资料不足以支持，报告写入研究限制"]
    return RecoveryPlan(
        need_code=need_code,
        strategy=strategy,
        gap_class=cls,
        actions=tuple(actions),
        requires_supplementary=strategy == RecoveryStrategy.SUPPLEMENTARY_DISCOVERY,
        requires_conflict_adjudication=strategy == RecoveryStrategy.CONFLICT_ADJUDICATE,
    )


@dataclass
class RecoveryResultTracker:
    """逐 gap 恢复结果计数（smoke 前后对照；只增不减，不隐藏缺口）。"""

    initial_issue_count: int = 0
    recovered: int = 0
    resolved_conflict: int = 0
    wording_fixed: int = 0
    true_missing: int = 0
    unresolved: int = 0

    def record(self, category: RecoveryCategory) -> None:
        if category == RecoveryCategory.RECOVERED:
            self.recovered += 1
        elif category == RecoveryCategory.RESOLVED_CONFLICT:
            self.resolved_conflict += 1
        elif category == RecoveryCategory.WORDING_FIXED:
            self.wording_fixed += 1
        elif category == RecoveryCategory.TRUE_MISSING:
            self.true_missing += 1
        else:
            self.unresolved += 1

    def as_dict(self) -> dict:
        return {
            "initial_issue_count": self.initial_issue_count,
            "recovered": self.recovered,
            "resolved_conflict": self.resolved_conflict,
            "wording_fixed": self.wording_fixed,
            "true_missing": self.true_missing,
            "unresolved": self.unresolved,
        }


def adjudicated_strategy(
    plan: RecoveryPlan,
    *,
    financial_succeeded: bool = False,
    conflict_succeeded: bool = False,
    wording_applied: bool = False,
    supplementary_succeeded: bool = False,
    rounds_done: int = 0,
    round_cap: int = 1,
) -> RecoveryCategory:
    """P1.10 出口：只有全部主恢复路径尝试且失败后才判 TRUE_MISSING。"""
    if plan.strategy == RecoveryStrategy.CONFLICT_ADJUDICATE:
        return (
            RecoveryCategory.RESOLVED_CONFLICT
            if conflict_succeeded
            else RecoveryCategory.UNRESOLVED
        )
    if plan.strategy == RecoveryStrategy.WORDING_REWRITE:
        return RecoveryCategory.WORDING_FIXED if wording_applied else RecoveryCategory.UNRESOLVED
    # 检索/抽取/补源类缺口：任一主路径成功即 RECOVERED。
    if financial_succeeded or supplementary_succeeded:
        return RecoveryCategory.RECOVERED
    # 未用尽恢复轮次 -> 继续找，不臆断数据不存在。
    if rounds_done < round_cap:
        return RecoveryCategory.UNRESOLVED
    # 轮次用尽且无成功：纯补源型缺口 -> 真实缺失；其余路径用 recovery_exhausted 判定。
    if plan.strategy == RecoveryStrategy.SUPPLEMENTARY_DISCOVERY:
        return (
            RecoveryCategory.TRUE_MISSING
            if not supplementary_succeeded
            else RecoveryCategory.UNRESOLVED
        )
    exhausted = recovery_exhausted(
        [
            ("existing_source", financial_succeeded),
            ("financial", financial_succeeded),
            ("supplementary", supplementary_succeeded),
        ]
    )
    return RecoveryCategory.TRUE_MISSING if exhausted else RecoveryCategory.UNRESOLVED
