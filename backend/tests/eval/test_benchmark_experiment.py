"""Benchmark experiment planning + output rendering unit tests (stage 7B.1.4D).

不依赖 PG / Chroma / LLM：只覆盖冻结计划矩阵、执行配置绑定与
JSON/Markdown/CSV 渲染。
"""

from app.eval.benchmark.experiment import (
    _render_csv,
    _render_markdown,
    make_config,
    plan_payload_for,
)
from app.eval.variants import EvalVariantId
from app.eval.variants.insightforge_full import INSIGHTFORGE_FULL_PROMPT_VERSION
from app.eval.variants.multi_stage_no_audit import MULTI_STAGE_NO_AUDIT_PROMPT_VERSION
from app.eval.variants.single_rag import SINGLE_RAG_PROMPT_VERSION


def test_make_config_binds_variant_and_prompt_version() -> None:
    expected_prompt = {
        EvalVariantId.SINGLE_RAG: SINGLE_RAG_PROMPT_VERSION,
        EvalVariantId.MULTI_STAGE_NO_AUDIT: MULTI_STAGE_NO_AUDIT_PROMPT_VERSION,
        EvalVariantId.INSIGHTFORGE_FULL: INSIGHTFORGE_FULL_PROMPT_VERSION,
    }
    for variant_id in EvalVariantId:
        config = make_config(variant_id)
        assert config.variant_id == variant_id
        assert config.prompt_version == expected_prompt[variant_id]
        assert config.model.provider == "deepseek"
        assert config.model.model_id == "deepseek-v4-flash"
        assert config.model.thinking_enabled is False
        assert config.retrieval_top_k == (5 if variant_id == EvalVariantId.INSIGHTFORGE_FULL else 3)


def test_plan_payload_for_single_rag_is_none() -> None:
    for case_id in ("moutai-business", "moutai-financial", "moutai-full"):
        assert plan_payload_for(EvalVariantId.SINGLE_RAG, case_id) is None


def test_plan_payload_for_document_only_cases() -> None:
    for variant_id in (
        EvalVariantId.MULTI_STAGE_NO_AUDIT,
        EvalVariantId.INSIGHTFORGE_FULL,
    ):
        for case_id in ("moutai-business", "moutai-financial"):
            payload = plan_payload_for(variant_id, case_id)
            assert payload is not None
            assert payload.document_needs
            assert not payload.financial_needs
            assert not payload.macro_needs
            assert not payload.valuation_needs


def test_plan_payload_for_full_case() -> None:
    payload = plan_payload_for(EvalVariantId.INSIGHTFORGE_FULL, "moutai-full")
    assert payload is not None
    assert payload.document_needs
    assert payload.financial_needs
    assert payload.macro_needs
    assert payload.valuation_needs
    # multi_stage 在 full case 上保持 document-only（input fail-fast 才是 honest 路径）。
    multi = plan_payload_for(EvalVariantId.MULTI_STAGE_NO_AUDIT, "moutai-full")
    assert multi is not None
    assert not multi.financial_needs and not multi.macro_needs and not multi.valuation_needs


def _sample_payload() -> dict:
    return {
        "dataset_id": "insightforge_a_share_benchmark",
        "dataset_version": 1,
        "as_of": "2025-08-01",
        "mode": "fake",
        "model": "deepseek:deepseek-v4-flash",
        "generated_at": "2025-01-01T00:00:00+00:00",
        "attempts": [
            {
                "case_id": "moutai-business",
                "variant_id": "single_rag",
                "attempt_no": 1,
                "status": "success",
                "error_code": None,
                "wall_latency_ms": 12,
                "execution_id": "e" * 32,
                "variant_output_fingerprint": "f" * 64,
                "usage_components": ["eval_single_rag_answer"],
                "usage_call_count": 1,
                "total_tokens": 40,
                "estimated_cost_usd": "0.000001",
                "citation_validity": {
                    "status": "computed",
                    "value": "1.0",
                    "numerator": "1",
                    "denominator": "1",
                },
                "citation_coverage": {
                    "status": "computed",
                    "value": "1.0",
                    "numerator": "1",
                    "denominator": "1",
                },
                "persisted": True,
                "expected_fail_fast": False,
                "notes": ["execution_persisted", "scoring_persisted"],
            },
            {
                "case_id": "moutai-full",
                "variant_id": "single_rag",
                "attempt_no": 1,
                "status": "failed",
                "error_code": "single_rag_input_not_supported",
                "wall_latency_ms": 3,
                "execution_id": "d" * 32,
                "variant_output_fingerprint": None,
                "usage_components": [],
                "usage_call_count": 0,
                "total_tokens": None,
                "estimated_cost_usd": None,
                "citation_validity": None,
                "citation_coverage": None,
                "persisted": False,
                "expected_fail_fast": True,
                "notes": ["fail_fast_as_expected"],
            },
        ],
    }


def test_render_markdown_contains_rows() -> None:
    text = _render_markdown(_sample_payload())
    assert "# InsightForge 三路 Variant Benchmark 摘要" in text
    assert "moutai-business" in text
    assert "single_rag" in text
    assert "single_rag_input_not_supported" in text
    assert "computed" in text


def test_render_csv_quotes_and_roundtrips() -> None:
    csv_text = _render_csv(_sample_payload())
    lines = csv_text.strip().splitlines()
    assert lines[0].startswith("case_id,variant_id")
    assert len(lines) == 3  # header + 2 attempts
    assert "single_rag_input_not_supported" in csv_text
    # CSV 可被 python csv 解析回同样行数。
    import csv
    import io

    parsed = list(csv.reader(io.StringIO(csv_text)))
    assert len(parsed) == 3
    assert parsed[1][0] == "moutai-business"
    assert parsed[2][3] == "failed"
