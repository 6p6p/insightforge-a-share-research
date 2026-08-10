"""Prompt 契约单元测试 (stage 4D.1B).

验证：
- system / data 分离：Claim Pack 只进 user（delimiter 包裹），system 固定且无插值；
- 最小上下文：research_question + analysis_as_of + company_name + Claim Pack；
  **不含 UUID / fingerprint / evidence id / raw response**；
- extract_claim_pack_data 往返；
- research_question 空白 / 空 claim pack → SynthesisAnalysisInputError。
"""

from datetime import date
from uuid import uuid4

import pytest

from app.analysis.synthesis.contracts import SynthesisAnalysisContext
from app.analysis.synthesis.errors import SynthesisAnalysisInputError
from app.analysis.synthesis.packs import build_claim_pack
from app.analysis.synthesis.prompt import (
    CLAIM_PACK_END,
    CLAIM_PACK_START,
    SYNTHESIS_ANALYSIS_SYSTEM_PROMPT,
    build_analysis_messages,
    extract_claim_pack_data,
)
from app.claims.contracts import (
    ClaimAnalysisDomain,
    ClaimConfidence,
    ClaimImportance,
    ClaimKind,
)
from app.synthesis.contracts import VerifiedSynthesisClaim

_CUTOFF = date(2026, 8, 10)


def _claim(statement: str = "贵州茅台2026年营收同比增长15%。") -> VerifiedSynthesisClaim:
    return VerifiedSynthesisClaim(
        claim_id=uuid4(),
        claim_fingerprint="0" * 64,
        company_id=uuid4(),
        research_question_sha256="1" * 64,
        analysis_domain=ClaimAnalysisDomain.BUSINESS,
        claim_kind=ClaimKind.FACT,
        statement=statement,
        confidence=ClaimConfidence.HIGH,
        importance=ClaimImportance.NORMAL,
        claim_schema_version=1,
        analyst_name="test-analyst",
        analyst_version=1,
        analyst_model_id=None,
        evidence_card_ids=[uuid4(), uuid4()],
        domain_analysis_as_of=None,
    )


def _context() -> SynthesisAnalysisContext:
    return SynthesisAnalysisContext(
        research_question="贵州茅台2026年营收与估值是否合理？",
        analysis_as_of=_CUTOFF,
        strategy="分析重点：结构化综合。",
    )


def _messages() -> list[dict[str, str]]:
    pack = build_claim_pack(
        research_question=_context().research_question,
        analysis_as_of=_CUTOFF,
        company_name="贵州茅台",
        claims=[_claim(), _claim("2026年贵州茅台净利润同比增长15%。")],
    )
    return build_analysis_messages(context=_context(), claim_pack=pack)


class TestBuildAnalysisMessages:
    def test_system_role_is_frozen_prompt(self) -> None:
        messages = _messages()
        assert [m["role"] for m in messages] == ["system", "user"]
        assert messages[0]["content"] == SYNTHESIS_ANALYSIS_SYSTEM_PROMPT

    def test_user_payload_is_data_delimited(self) -> None:
        user = _messages()[1]["content"]
        assert CLAIM_PACK_START in user
        assert CLAIM_PACK_END in user
        # delimiter 必须包裹 Claim Pack，且是数据而非 system 指令。
        assert user.index(CLAIM_PACK_START) < user.index(CLAIM_PACK_END)
        extracted = extract_claim_pack_data(user)
        assert "贵州茅台2026年营收同比增长15%。" in extracted
        assert "C1" in extracted and "C2" in extracted

    def test_no_internal_identifiers_in_prompt(self) -> None:
        # LLM 永不看 UUID / fingerprint / evidence id。
        for message in _messages():
            assert "0" * 64 not in message["content"]
            assert "sha256" not in message["content"].lower()

    def test_rejects_blank_research_question(self) -> None:
        pack = build_claim_pack(
            research_question="q",
            analysis_as_of=_CUTOFF,
            company_name="贵州茅台",
            claims=[_claim()],
        )
        context = SynthesisAnalysisContext(
            research_question="   ", analysis_as_of=_CUTOFF, strategy="s"
        )
        with pytest.raises(SynthesisAnalysisInputError):
            build_analysis_messages(context=context, claim_pack=pack)

    def test_rejects_empty_claim_pack(self) -> None:
        pack = build_claim_pack(
            research_question="q",
            analysis_as_of=_CUTOFF,
            company_name="贵州茅台",
            claims=[],
        )
        with pytest.raises(SynthesisAnalysisInputError):
            build_analysis_messages(context=_context(), claim_pack=pack)


def test_extract_claim_pack_data_roundtrip() -> None:
    pack = build_claim_pack(
        research_question="q",
        analysis_as_of=_CUTOFF,
        company_name="贵州茅台",
        claims=[_claim()],
    )
    messages = build_analysis_messages(context=_context(), claim_pack=pack)
    extracted = extract_claim_pack_data(messages[1]["content"])
    assert "C1" in extracted
    assert CLAIM_PACK_START not in extracted
    assert CLAIM_PACK_END not in extracted
