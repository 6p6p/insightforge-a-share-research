"""Deterministic cross-variant metrics tests (stage 7B.1.2A).

覆盖 citation_validity / citation_coverage 两个确定性指标 + 结构校验 +
registry。零 DB / LLM / network：全部用内存 frozen contract 构造 context。
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.eval.contracts import (
    EvalCitation,
    EvalClaim,
    EvalVariantOutput,
    FrozenDocumentSourceRef,
    FrozenMacroSnapshotRef,
    FrozenSourceSnapshot,
    FrozenStructuredArtifactRef,
    StructuredArtifactType,
)
from app.eval.errors import EvalOutputStructureError, EvalScoringError
from app.eval.metrics import MetricName, MetricStatus
from app.eval.scoring import (
    CitationCoverageCalculator,
    CitationValidityCalculator,
    EvalScoringContext,
    calculate_available_deterministic_metrics,
    get_deterministic_calculator,
    verify_variant_output_structure,
)
from app.eval.variants import EvalVariantId

_DOC_SHA = "a" * 64
_MACRO_FP = "b" * 64
_STRUCT_FP = "c" * 64
_EXEC_FP = "d" * 64
_UNKNOWN_FP = "f" * 64


def _snapshot() -> FrozenSourceSnapshot:
    return FrozenSourceSnapshot(
        document_sources=(
            FrozenDocumentSourceRef(
                source_record_id=uuid4(),
                raw_artifact_id=uuid4(),
                content_sha256=_DOC_SHA,
                provider_key="xinhuanet",
                document_type="news_article",
                media_type="text/html",
            ),
        ),
        macro_snapshots=(
            FrozenMacroSnapshotRef(
                snapshot_id=uuid4(),
                series_id=uuid4(),
                snapshot_fingerprint=_MACRO_FP,
                payload_sha256="e" * 64,
                fetched_at=datetime(2026, 8, 9, tzinfo=UTC),
            ),
        ),
        structured_artifacts=(
            FrozenStructuredArtifactRef(
                artifact_type=StructuredArtifactType.FINANCIAL_METRIC_OBSERVATION,
                artifact_id=uuid4(),
                artifact_fingerprint=_STRUCT_FP,
                payload_sha256="e" * 64,
            ),
        ),
    )


def _output(*, claims=(), citations=()) -> EvalVariantOutput:
    return EvalVariantOutput(
        variant_id=EvalVariantId.INSIGHTFORGE_FULL,
        case_id="moutai",
        case_version=1,
        final_text="final text",
        claims=claims,
        citations=citations,
    )


def _context(output: EvalVariantOutput) -> EvalScoringContext:
    return EvalScoringContext(
        execution_spec_fingerprint=_EXEC_FP,
        variant_output=output,
        source_snapshot=_snapshot(),
    )


def _claim(cid: str, citation_ids=()) -> EvalClaim:
    return EvalClaim(
        claim_id=cid,
        statement=f"statement {cid}",
        claim_type="fact",
        citation_ids=citation_ids,
    )


def _citation(cid: str, *, source: str = _DOC_SHA, claim_ids=()) -> EvalCitation:
    return EvalCitation(citation_id=cid, source_fingerprint=source, claim_ids=claim_ids)


# ---------------------------------------------------------------- citation_validity


def test_citation_validity_all_valid() -> None:
    output = _output(
        claims=(_claim("c1", ("ci1",)), _claim("c2", ("ci2",))),
        citations=(
            _citation("ci1", source=_DOC_SHA, claim_ids=("c1",)),
            _citation("ci2", source=_MACRO_FP, claim_ids=("c2",)),
        ),
    )
    value = CitationValidityCalculator().calculate(_context(output))
    assert value.status == MetricStatus.COMPUTED
    assert value.value == Decimal("1")
    assert value.numerator == Decimal("2")
    assert value.denominator == Decimal("2")
    assert value.sample_count == 2


def test_citation_validity_unknown_source_invalid() -> None:
    output = _output(
        citations=(_citation("ci1", source=_DOC_SHA), _citation("ci2", source=_UNKNOWN_FP)),
    )
    value = CitationValidityCalculator().calculate(_context(output))
    assert value.status == MetricStatus.COMPUTED
    assert value.numerator == Decimal("1")
    assert value.denominator == Decimal("2")
    assert value.value == Decimal("1") / Decimal("2")


def test_citation_validity_dangling_claim_invalid() -> None:
    output = _output(
        claims=(_claim("c1"),),
        citations=(
            _citation("ci1", source=_DOC_SHA, claim_ids=("c1",)),
            _citation("ci2", source=_MACRO_FP, claim_ids=("missing",)),
        ),
    )
    value = CitationValidityCalculator().calculate(_context(output))
    assert value.numerator == Decimal("1")
    assert value.denominator == Decimal("2")


def test_citation_validity_duplicate_id_invalid() -> None:
    output = _output(
        citations=(_citation("dup", source=_DOC_SHA), _citation("dup", source=_MACRO_FP)),
    )
    value = CitationValidityCalculator().calculate(_context(output))
    assert value.status == MetricStatus.COMPUTED
    assert value.value == Decimal("0")
    assert value.numerator == Decimal("0")
    assert value.denominator == Decimal("2")


def test_citation_validity_not_applicable_when_no_citations() -> None:
    output = _output(claims=(_claim("c1"),))
    value = CitationValidityCalculator().calculate(_context(output))
    assert value.status == MetricStatus.NOT_APPLICABLE
    assert value.value is None
    assert value.reason_code == "no_citations"


# ---------------------------------------------------------------- citation_coverage


def test_citation_coverage_all_covered() -> None:
    output = _output(
        claims=(_claim("c1", ("ci1",)), _claim("c2", ("ci2",))),
        citations=(
            _citation("ci1", source=_DOC_SHA, claim_ids=("c1",)),
            _citation("ci2", source=_MACRO_FP, claim_ids=("c2",)),
        ),
    )
    value = CitationCoverageCalculator().calculate(_context(output))
    assert value.status == MetricStatus.COMPUTED
    assert value.value == Decimal("1")
    assert value.numerator == Decimal("2")
    assert value.denominator == Decimal("2")


def test_citation_coverage_partial() -> None:
    output = _output(
        claims=(
            _claim("c1", ("ci1",)),
            _claim("c2"),  # 无 citation
            _claim("c3", ("ci_bad",)),  # citation 指向未知 source → invalid
        ),
        citations=(
            _citation("ci1", source=_DOC_SHA, claim_ids=("c1",)),
            _citation("ci_bad", source=_UNKNOWN_FP, claim_ids=("c3",)),
        ),
    )
    value = CitationCoverageCalculator().calculate(_context(output))
    assert value.status == MetricStatus.COMPUTED
    assert value.numerator == Decimal("1")
    assert value.denominator == Decimal("3")
    assert value.value == Decimal("1") / Decimal("3")


def test_citation_coverage_not_applicable_when_no_claims() -> None:
    output = _output(citations=(_citation("ci1", source=_DOC_SHA),))
    value = CitationCoverageCalculator().calculate(_context(output))
    assert value.status == MetricStatus.NOT_APPLICABLE
    assert value.value is None
    assert value.reason_code == "no_claims"


# ---------------------------------------------------------------- 结构校验


def test_verify_structure_passes_on_well_formed() -> None:
    output = _output(
        claims=(_claim("c1", ("ci1",)),),
        citations=(_citation("ci1", source=_DOC_SHA, claim_ids=("c1",)),),
    )
    assert verify_variant_output_structure(_context(output)) is None


def test_verify_structure_rejects_duplicate_claim_id() -> None:
    output = _output(claims=(_claim("dup"), _claim("dup")))
    with pytest.raises(EvalOutputStructureError, match="duplicate claim_id"):
        verify_variant_output_structure(_context(output))


def test_verify_structure_rejects_dangling_claim_citation_ref() -> None:
    output = _output(claims=(_claim("c1", ("ci_missing",)),))
    with pytest.raises(EvalOutputStructureError, match="claim citation_id not in citations"):
        verify_variant_output_structure(_context(output))


def test_verify_structure_rejects_unknown_source() -> None:
    output = _output(citations=(_citation("ci1", source=_UNKNOWN_FP),))
    with pytest.raises(EvalOutputStructureError, match="not in snapshot"):
        verify_variant_output_structure(_context(output))


# ---------------------------------------------------------------- registry


def test_available_deterministic_metrics_exact() -> None:
    assert calculate_available_deterministic_metrics() == (
        MetricName.CITATION_VALIDITY,
        MetricName.CITATION_COVERAGE,
    )


def test_get_calculator_unsupported_raises() -> None:
    assert get_deterministic_calculator(MetricName.CITATION_VALIDITY).name == (
        MetricName.CITATION_VALIDITY
    )
    with pytest.raises(EvalScoringError):
        get_deterministic_calculator(MetricName.CLAIM_SUPPORT_RATE)
    with pytest.raises(EvalScoringError):
        get_deterministic_calculator(MetricName.UNSUPPORTED_CLAIM_RATIO)
