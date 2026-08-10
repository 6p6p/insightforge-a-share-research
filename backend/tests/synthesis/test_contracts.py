"""Claim synthesis input contracts unit tests (stage 4D.1A, spec V).

纯函数校验（无 DB / 无网络 / 无 LLM）：
- SynthesisInputDraft 构造校验（research_question trim 非空、claim_ids 2..50
  去重 canonical 排序、company_id / analysis_as_of 类型）；
- compute_synthesis_fingerprint 确定性（同 input → 同 fp；claim 顺序无关；
  question / cutoff / claim set / claim fingerprint 任一变化 → 新 fp；不含
  synthesis_id / created_at）；
- build_synthesis_input_summary 计数（全确定性，key 缺失补 0）；
- resolve_availability no-lookahead 的 None 映射（provenance 缺失 → 无法解析
  → 集成路径映射为 SynthesisTemporalEvidenceInsufficient）。
"""

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest

from app.claims.contracts import (
    ClaimAnalysisDomain,
    ClaimConfidence,
    ClaimImportance,
    ClaimKind,
)
from app.claims.macro_policy import resolve_availability
from app.synthesis.contracts import (
    CLAIM_SYNTHESIS_SCHEMA_VERSION,
    MAX_SYNTHESIS_CLAIMS,
    MIN_SYNTHESIS_CLAIMS,
    SynthesisInputDraft,
    VerifiedSynthesisClaim,
    build_synthesis_input_summary,
    compute_synthesis_fingerprint,
)
from app.synthesis.errors import SynthesisDraftError

_COMPANY = uuid4()
_QUESTION = "贵州茅台2026年营收与估值是否合理？"
_CUTOFF = date(2026, 8, 10)


def _claim(
    claim_id: UUID | None = None,
    *,
    domain: ClaimAnalysisDomain = ClaimAnalysisDomain.BUSINESS,
    kind: ClaimKind = ClaimKind.FACT,
    confidence: ClaimConfidence = ClaimConfidence.HIGH,
    importance: ClaimImportance = ClaimImportance.NORMAL,
    schema_version: int = 1,
    fingerprint: str | None = None,
    company_id: UUID | None = None,
) -> VerifiedSynthesisClaim:
    return VerifiedSynthesisClaim(
        claim_id=claim_id if claim_id is not None else uuid4(),
        claim_fingerprint=fingerprint if fingerprint is not None else "0" * 64,
        company_id=company_id if company_id is not None else _COMPANY,
        research_question_sha256="1" * 64,
        analysis_domain=domain,
        claim_kind=kind,
        statement="陈述",
        confidence=confidence,
        importance=importance,
        claim_schema_version=schema_version,
        analyst_name="test-analyst",
        analyst_version=1,
        analyst_model_id=None,
        evidence_card_ids=[],
        domain_analysis_as_of=None,
    )


def _draft(claim_ids: list[UUID]) -> SynthesisInputDraft:
    return SynthesisInputDraft(
        company_id=_COMPANY,
        research_question=_QUESTION,
        analysis_as_of=_CUTOFF,
        claim_ids=claim_ids,
    )


# ---------------------------------------------------------------- draft 构造


class TestSynthesisInputDraft:
    def test_trims_research_question(self) -> None:
        draft = _draft([uuid4(), uuid4()])
        # 构造时已 trim。
        padded = SynthesisInputDraft(
            company_id=_COMPANY,
            research_question="  贵州茅台2026年营收与估值是否合理？  ",
            analysis_as_of=_CUTOFF,
            claim_ids=[uuid4(), uuid4()],
        )
        assert padded.research_question == _QUESTION
        assert draft.research_question == _QUESTION

    def test_rejects_blank_research_question(self) -> None:
        with pytest.raises(SynthesisDraftError, match="research_question"):
            SynthesisInputDraft(
                company_id=_COMPANY,
                research_question="   ",
                analysis_as_of=_CUTOFF,
                claim_ids=[uuid4(), uuid4()],
            )

    def test_rejects_empty_research_question(self) -> None:
        with pytest.raises(SynthesisDraftError, match="research_question"):
            SynthesisInputDraft(
                company_id=_COMPANY,
                research_question="",
                analysis_as_of=_CUTOFF,
                claim_ids=[uuid4(), uuid4()],
            )

    def test_normalizes_claim_ids_dedup_and_canonical_sort(self) -> None:
        a, b = uuid4(), uuid4()
        # 提交顺序乱 + 重复 → 去重 + str(uuid) 升序。
        draft = _draft([b, a, b, a])
        assert draft.claim_ids == sorted([a, b], key=str)
        assert len(draft.claim_ids) == 2

    def test_rejects_non_uuid_claim_id(self) -> None:
        with pytest.raises(SynthesisDraftError, match="UUID"):
            SynthesisInputDraft(
                company_id=_COMPANY,
                research_question=_QUESTION,
                analysis_as_of=_CUTOFF,
                claim_ids=[uuid4(), "not-a-uuid"],
            )

    def test_rejects_bool_company_id(self) -> None:
        with pytest.raises(SynthesisDraftError, match="UUID"):
            SynthesisInputDraft(
                company_id=True,
                research_question=_QUESTION,
                analysis_as_of=_CUTOFF,
                claim_ids=[uuid4(), uuid4()],
            )

    def test_rejects_non_date_analysis_as_of(self) -> None:
        with pytest.raises(SynthesisDraftError, match="analysis_as_of"):
            SynthesisInputDraft(
                company_id=_COMPANY,
                research_question=_QUESTION,
                analysis_as_of="2026-08-10",
                claim_ids=[uuid4(), uuid4()],
            )

    def test_rejects_less_than_min_claims(self) -> None:
        with pytest.raises(SynthesisDraftError, match=str(MIN_SYNTHESIS_CLAIMS)):
            _draft([uuid4()])

    def test_accepts_exact_min_claims(self) -> None:
        draft = _draft([uuid4() for _ in range(MIN_SYNTHESIS_CLAIMS)])
        assert len(draft.claim_ids) == MIN_SYNTHESIS_CLAIMS

    def test_accepts_exact_max_claims(self) -> None:
        draft = _draft([uuid4() for _ in range(MAX_SYNTHESIS_CLAIMS)])
        assert len(draft.claim_ids) == MAX_SYNTHESIS_CLAIMS

    def test_rejects_more_than_max_claims(self) -> None:
        with pytest.raises(SynthesisDraftError, match=str(MAX_SYNTHESIS_CLAIMS)):
            _draft([uuid4() for _ in range(MAX_SYNTHESIS_CLAIMS + 1)])


# ---------------------------------------------------------------- fingerprint


class TestSynthesisFingerprint:
    def _fp(self, claims: list[VerifiedSynthesisClaim]) -> str:
        return compute_synthesis_fingerprint(
            synthesis_schema_version=CLAIM_SYNTHESIS_SCHEMA_VERSION,
            company_id=_COMPANY,
            research_question=_QUESTION,
            research_question_sha256="1" * 64,
            analysis_as_of=_CUTOFF,
            claims=claims,
        )

    def test_deterministic_same_input(self) -> None:
        claims = [_claim(), _claim()]
        assert self._fp(claims) == self._fp(claims)
        assert len(self._fp(claims)) == 64

    def test_input_order_independent(self) -> None:
        a, b = _claim(), _claim()
        assert self._fp([a, b]) == self._fp([b, a])

    def test_fingerprint_trusts_normalized_claim_set(self) -> None:
        # 纯函数不自行去重：重复的 claim entry 会产生不同 fp。调用方必须先经
        # SynthesisInputDraft 规范化（去重 + canonical 排序）再进入指纹——
        # 该去重行为由 test_normalizes_claim_ids_dedup_and_canonical_sort 覆盖。
        a = _claim()
        assert self._fp([a, a]) != self._fp([a])

    def test_changes_with_question(self) -> None:
        claims = [_claim(), _claim()]
        base = self._fp(claims)
        other = compute_synthesis_fingerprint(
            synthesis_schema_version=CLAIM_SYNTHESIS_SCHEMA_VERSION,
            company_id=_COMPANY,
            research_question=_QUESTION + "？",
            research_question_sha256="2" * 64,
            analysis_as_of=_CUTOFF,
            claims=claims,
        )
        assert base != other

    def test_changes_with_cutoff(self) -> None:
        claims = [_claim(), _claim()]
        base = self._fp(claims)
        other = compute_synthesis_fingerprint(
            synthesis_schema_version=CLAIM_SYNTHESIS_SCHEMA_VERSION,
            company_id=_COMPANY,
            research_question=_QUESTION,
            research_question_sha256="1" * 64,
            analysis_as_of=date(2026, 8, 11),
            claims=claims,
        )
        assert base != other

    def test_changes_with_claim_set(self) -> None:
        two = self._fp([_claim(), _claim()])
        three = self._fp([_claim(), _claim(), _claim()])
        assert two != three

    def test_changes_with_claim_fingerprint(self) -> None:
        a, b = _claim(), _claim()
        base = self._fp([a, b])
        altered = _claim(claim_id=b.claim_id, fingerprint="f" * 64)
        assert base != self._fp([a, altered])

    def test_changes_with_claim_domain(self) -> None:
        a, b = _claim(), _claim()
        base = self._fp([a, b])
        altered = _claim(
            claim_id=b.claim_id,
            domain=ClaimAnalysisDomain.VALUATION,
            schema_version=7,
        )
        assert base != self._fp([a, altered])

    def test_changes_with_claim_kind(self) -> None:
        a, b = _claim(), _claim()
        base = self._fp([a, b])
        altered = _claim(claim_id=b.claim_id, kind=ClaimKind.RISK)
        assert base != self._fp([a, altered])

    def test_stable_across_recomputation_no_synthesis_id(self) -> None:
        # 同一语义输入重新派生 → 同一 fp；指纹只由确定性输入决定（不含
        # synthesis_id / created_at，函数签名本身也不接收它们）。用同一组
        # claim（相同 id / fingerprint）两次计算 → 完全一致。
        claims = [_claim(), _claim()]
        assert self._fp(claims) == self._fp(claims)


# ---------------------------------------------------------------- summary


class TestSynthesisInputSummary:
    def test_counts_deterministic(self) -> None:
        claims = [
            _claim(domain=ClaimAnalysisDomain.BUSINESS, kind=ClaimKind.FACT),
            _claim(
                domain=ClaimAnalysisDomain.MACRO,
                kind=ClaimKind.RISK,
                confidence=ClaimConfidence.MEDIUM,
            ),
            _claim(
                domain=ClaimAnalysisDomain.VALUATION,
                kind=ClaimKind.RELATIVE_VALUATION,
                importance=ClaimImportance.CRITICAL,
                schema_version=7,
            ),
        ]
        summary = build_synthesis_input_summary(claims)
        assert summary.claim_count == 3
        assert summary.domain_counts == {"business": 1, "macro": 1, "valuation": 1}
        assert summary.claim_kind_counts == {"fact": 1, "risk": 1, "relative_valuation": 1}
        assert summary.confidence_counts == {"high": 2, "medium": 1}
        assert summary.importance_counts == {"normal": 2, "critical": 1}

    def test_empty_summary(self) -> None:
        summary = build_synthesis_input_summary([])
        assert summary.claim_count == 0
        assert summary.domain_counts == {}
        assert summary.claim_kind_counts == {}
        assert summary.confidence_counts == {}
        assert summary.importance_counts == {}

    def test_counts_never_mutate_input(self) -> None:
        claims = [_claim(), _claim()]
        before = [(c.claim_id, c.claim_fingerprint) for c in claims]
        build_synthesis_input_summary(claims)
        after = [(c.claim_id, c.claim_fingerprint) for c in claims]
        assert after == before


# ---------------------------------------------------------------- temporal


def test_temporal_insufficient_maps_unresolvable_availability() -> None:
    """无法解析 availability（provenance 缺失）→ 拒绝，不伪造缺失日期。

    当前 schema 用 RESTRICT FK 保证 provenance 行不可悬空，因此
    SynthesisTemporalEvidenceInsufficient 是集成路径的防御性分支；此处直接
    验证 resolve_availability 的 None 映射（spec O）。
    """
    assert (
        resolve_availability(
            origin_type="document_chunk",
            snapshot_fetched_at=None,
            source_published_at=None,
            source_acquired_at=None,
        )
        is None
    )
    assert (
        resolve_availability(
            origin_type="macro_observation",
            snapshot_fetched_at=None,
            source_published_at=datetime.now(UTC),
            source_acquired_at=datetime.now(UTC),
        )
        is None
    )
