"""EvalVariantId / COMPARABLE_VARIANTS 契约测试（stage 7B.1.0）。"""

from app.eval.variants import COMPARABLE_VARIANTS, EvalVariantId


def test_exactly_three_comparable_variants() -> None:
    assert len(EvalVariantId) == 3
    assert len(COMPARABLE_VARIANTS) == 3


def test_variant_order_stable() -> None:
    assert list(EvalVariantId) == ["single_rag", "multi_stage_no_audit", "insightforge_full"]
    assert COMPARABLE_VARIANTS == tuple(EvalVariantId)


def test_noop_test_mock_not_variant_id() -> None:
    values = {v.value for v in EvalVariantId}
    for forbidden in ("noop", "test", "mock"):
        assert forbidden not in values
