"""P1.1/1.10 recovery orchestration tests（确定性，0 IO）。"""

from app.research_backflow.recovery import GapClass
from app.research_backflow.recovery_orchestrator import (
    RecoveryCategory,
    RecoveryResultTracker,
    RecoveryStrategy,
    adjudicated_strategy,
    build_recovery_plan,
)


def test_build_plan_source_gap():
    plan = build_recovery_plan("x", has_source=False, has_chunk=False)
    assert plan.strategy == RecoveryStrategy.SUPPLEMENTARY_DISCOVERY
    assert plan.requires_supplementary is True
    assert plan.gap_class == GapClass.SOURCE_GAP


def test_build_plan_retrieval_miss():
    plan = build_recovery_plan("x", has_source=True, has_chunk=False)
    assert plan.strategy == RecoveryStrategy.RETRY_RETRIEVAL
    assert plan.gap_class == GapClass.RETRIEVAL_MISS


def test_build_plan_extraction_miss():
    plan = build_recovery_plan("x", has_source=True, has_chunk=True)
    assert plan.strategy == RecoveryStrategy.RE_EXTRACT
    assert plan.gap_class == GapClass.EXTRACTION_MISS


def test_build_plan_wording_overclaim():
    plan = build_recovery_plan("x", has_source=True, has_chunk=True, wording_overclaim=True)
    assert plan.strategy == RecoveryStrategy.WORDING_REWRITE
    assert plan.requires_wording_rewrite is True


def test_plan_has_evidence_not_classified():
    plan = build_recovery_plan("x", has_source=True, has_chunk=True, has_evidence=True)
    assert plan.gap_class is None


def test_adjudicate_recovered():
    plan = build_recovery_plan("x", has_source=True, has_chunk=False)
    assert adjudicated_strategy(plan, financial_succeeded=True) == RecoveryCategory.RECOVERED


def test_adjudicate_true_missing_only_after_exhaustion():
    plan = build_recovery_plan("x", has_source=True, has_chunk=True)
    # 未达轮次上限 -> 不判不存在
    assert adjudicated_strategy(plan, rounds_done=0, round_cap=3) == RecoveryCategory.UNRESOLVED
    # 三条主路径尝试且失败 -> 才 TRUE_MISSING
    assert (
        adjudicated_strategy(
            plan,
            financial_succeeded=False,
            supplementary_succeeded=False,
            rounds_done=3,
            round_cap=3,
        )
        == RecoveryCategory.TRUE_MISSING
    )
    # 任一成功 -> 不判不存在
    assert (
        adjudicated_strategy(
            plan,
            financial_succeeded=True,
            supplementary_succeeded=False,
            rounds_done=3,
            round_cap=3,
        )
        != RecoveryCategory.TRUE_MISSING
    )


def test_tracker_counts():
    tr = RecoveryResultTracker(initial_issue_count=5)
    tr.record(RecoveryCategory.RECOVERED)
    tr.record(RecoveryCategory.RESOLVED_CONFLICT)
    tr.record(RecoveryCategory.WORDING_FIXED)
    tr.record(RecoveryCategory.TRUE_MISSING)
    tr.record(RecoveryCategory.TRUE_MISSING)
    tr.record(RecoveryCategory.UNRESOLVED)
    d = tr.as_dict()
    assert d["recovered"] == 1
    assert d["resolved_conflict"] == 1
    assert d["wording_fixed"] == 1
    assert d["true_missing"] == 2
    assert d["unresolved"] == 1
    assert d["initial_issue_count"] == 5

def test_executor_constructor_exposes_recovery_alias_model():
    # 回归：真实管线 AttributeError: '_recovery_alias_model'（构造函数多行锚点静默失败）。
    from app.research_backflow.executor import ResearchBackflowExecutor
    ex = ResearchBackflowExecutor(None, None, None)  # 仅构造，不触碰真实依赖
    assert hasattr(ex, "_recovery_alias_model")
    assert ex._recovery_alias_model is None
