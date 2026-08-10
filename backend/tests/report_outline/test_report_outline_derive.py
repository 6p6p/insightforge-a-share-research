"""Deterministic outline derivation + fingerprint tests (stage 5A, spec F-I).

纯函数测试（无 DB / 无 LLM）。覆盖：
- theme → theme section，按 persisted normalized order（themes 原顺序）；
- title 用 theme label，不重写；
- theme claim_ids = 非 duplicate canonical Claims，canonical（C alias）sort +
  dedupe；
- duplicate 非 canonical 成员从 theme 排除（canonical 留在 theme）；
- conflicts / evidence_gaps → 末尾追加 risks_and_gaps section（只存 indexes，
  不生成解释正文）；无则不加；
- section_id / section_order 顺序自洽；
- coverage 硬边界：未覆盖 input Claim → ReportOutlineClaimCoverageError；
  duplicate_ref 豁免；
- compute_outline_fingerprint：确定性；不含 outline_id / created_at；
  payload / result 变化 → 新指纹。
"""

from datetime import date
from uuid import UUID, uuid4

import pytest

from app.analysis.synthesis.contracts import (
    SynthesisAnalysisOutput,
    SynthesisClaimRole,
    SynthesisClaimRoleAssignment,
    SynthesisConflict,
    SynthesisDuplicate,
    SynthesisEvidenceGap,
    SynthesisPriority,
    SynthesisSeverity,
    SynthesisTheme,
    VerifiedSynthesisResult,
)
from app.report_outline.contracts import (
    OUTLINE_RISKS_AND_GAPS_TITLE,
    REPORT_OUTLINE_SCHEMA_VERSION,
    SECTION_TYPE_RISKS_AND_GAPS,
    SECTION_TYPE_THEME,
    compute_outline_fingerprint,
    parse_outline_sections,
)
from app.report_outline.derive import derive_outline_payload
from app.report_outline.errors import ReportOutlineClaimCoverageError, ReportOutlineIntegrityError

_QUESTION = "贵州茅台2026年营收与估值是否合理？"
_AS_OF = date(2026, 8, 10)


def _theme(title: str, refs: list[str]) -> SynthesisTheme:
    return SynthesisTheme(title=title, summary=f"{title} 摘要", claim_refs=refs)


def _roles(refs: list[str]) -> list[SynthesisClaimRoleAssignment]:
    return [
        SynthesisClaimRoleAssignment(
            claim_ref=ref, role=SynthesisClaimRole.SUPPORT, rationale=f"支持 {ref}"
        )
        for ref in refs
    ]


def _verified(claim_count: int, output: SynthesisAnalysisOutput) -> VerifiedSynthesisResult:
    claim_ids = [uuid4() for _ in range(claim_count)]
    alias_map = {f"C{i + 1}": claim_ids[i] for i in range(claim_count)}
    return VerifiedSynthesisResult(
        synthesis_result_id=uuid4(),
        synthesis_id=uuid4(),
        company_id=uuid4(),
        research_question=_QUESTION,
        research_question_sha256="b" * 64,
        analysis_as_of=_AS_OF,
        synthesis_fingerprint="c" * 64,
        result_fingerprint="d" * 64,
        input_claim_ids=tuple(claim_ids),
        alias_map=alias_map,
        output=output,
    )


def _output(
    themes: list[SynthesisTheme],
    *,
    duplicates: list[SynthesisDuplicate] | None = None,
    conflicts: list[SynthesisConflict] | None = None,
    evidence_gaps: list[SynthesisEvidenceGap] | None = None,
    all_refs: list[str] | None = None,
) -> SynthesisAnalysisOutput:
    refs = all_refs or sorted({r for t in themes for r in t.claim_refs}, key=lambda r: int(r[1:]))
    return SynthesisAnalysisOutput(
        summary="综合总结。",
        themes=themes,
        claim_roles=_roles(refs),
        duplicates=duplicates or [],
        conflicts=conflicts or [],
        evidence_gaps=evidence_gaps or [],
    )


def _dup(claim_refs: list[str], canonical_ref: str) -> SynthesisDuplicate:
    return SynthesisDuplicate(
        claim_refs=claim_refs, canonical_ref=canonical_ref, rationale="重复声明"
    )


def _conflict(claim_refs: list[str]) -> SynthesisConflict:
    return SynthesisConflict(
        claim_refs=claim_refs,
        description="冲突描述",
        severity=SynthesisSeverity.MEDIUM,
        resolution_direction="以更权威来源为准",
    )


def _gap(claim_ref: str) -> SynthesisEvidenceGap:
    return SynthesisEvidenceGap(
        description="缺少经营现金流证据",
        claim_refs=[claim_ref],
        suggested_evidence="经营现金流数据",
        priority=SynthesisPriority.MEDIUM,
    )


# ---------------------------------------------------------------- theme sections


def test_theme_sections_follow_persisted_order() -> None:
    verified = _verified(
        4,
        _output([_theme("营收增长", ["C2", "C1"]), _theme("财务稳健", ["C3", "C4"])]),
    )
    payload = derive_outline_payload(verified)

    sections = payload["sections"]
    assert [s["section_id"] for s in sections] == ["S1", "S2"]
    assert [s["section_order"] for s in sections] == [1, 2]
    # persisted normalized order：themes 原顺序。
    assert [s["title"] for s in sections] == ["营收增长", "财务稳健"]
    assert [s["section_type"] for s in sections] == [SECTION_TYPE_THEME] * 2
    # title 用 theme label，不重写。
    assert sections[0]["title"] == "营收增长"


def test_theme_claim_ids_canonical_sorted() -> None:
    verified = _verified(
        4,
        _output([_theme("多主题", ["C3", "C2", "C1", "C4"])]),
    )
    payload = derive_outline_payload(verified)
    section = payload["sections"][0]
    # 按 C alias（canonical）顺序解析回 claim_id。
    assert section["claim_ids"] == [
        str(verified.alias_map["C1"]),
        str(verified.alias_map["C2"]),
        str(verified.alias_map["C3"]),
        str(verified.alias_map["C4"]),
    ]


def test_duplicate_non_canonical_refs_excluded_from_theme() -> None:
    # C3 是重复声明（非 canonical），从 theme 排除；canonical C2 留在 theme。
    # coverage：C1/C2 在 theme，C3 经 duplicate_ref 豁免。
    verified = _verified(
        3,
        _output(
            [_theme("营收增长", ["C1", "C2", "C3"])],
            duplicates=[_dup(["C2", "C3"], canonical_ref="C2")],
        ),
    )
    payload = derive_outline_payload(verified)
    section = payload["sections"][0]
    assert section["claim_ids"] == [str(verified.alias_map["C1"]), str(verified.alias_map["C2"])]
    assert str(verified.alias_map["C3"]) not in section["claim_ids"]


# ---------------------------------------------------------------- risks_and_gaps section


def test_risks_and_gaps_section_appended_with_indexes() -> None:
    verified = _verified(
        2,
        _output(
            [_theme("营收增长", ["C1", "C2"])],
            conflicts=[_conflict(["C1", "C2"])],
            evidence_gaps=[_gap("C1"), _gap("C2")],
        ),
    )
    payload = derive_outline_payload(verified)
    sections = payload["sections"]

    last = sections[-1]
    assert last["section_type"] == SECTION_TYPE_RISKS_AND_GAPS
    assert last["title"] == OUTLINE_RISKS_AND_GAPS_TITLE
    assert last["section_order"] == 2
    assert last["section_id"] == "S2"
    # 只存 indexes，不生成解释正文；claim_ids 空。
    assert last["claim_ids"] == []
    assert last["conflict_indexes"] == [0]
    assert last["evidence_gap_indexes"] == [0, 1]
    # theme section 正常排在前面。
    assert sections[0]["section_type"] == SECTION_TYPE_THEME
    expected_theme_ids = [str(verified.alias_map["C1"]), str(verified.alias_map["C2"])]
    assert sections[0]["claim_ids"] == expected_theme_ids


def test_no_risks_and_gaps_section_when_none() -> None:
    verified = _verified(
        3,
        _output([_theme("营收增长", ["C1", "C2", "C3"])]),
    )
    payload = derive_outline_payload(verified)
    assert all(s["section_type"] == SECTION_TYPE_THEME for s in payload["sections"])


# ---------------------------------------------------------------- coverage boundary


def test_claim_coverage_error_when_claim_uncovered() -> None:
    # C5 不在任何 theme，也不是 duplicate_ref → coverage 硬边界拒绝。
    verified = _verified(
        5,
        _output([_theme("营收增长", ["C1", "C2", "C3", "C4"])]),
    )
    with pytest.raises(ReportOutlineClaimCoverageError):
        derive_outline_payload(verified)


def test_claim_coverage_exempt_via_duplicate_ref() -> None:
    # C5 不在 theme，但明确是 duplicate 组的非 canonical 成员 → 豁免；
    # C1-C4 在 theme（C2 是 canonical，留在 theme）。
    verified = _verified(
        5,
        _output(
            [_theme("营收增长", ["C1", "C2", "C3", "C4"])],
            duplicates=[_dup(["C2", "C5"], canonical_ref="C2")],
        ),
    )
    payload = derive_outline_payload(verified)
    section = payload["sections"][0]
    assert [s["section_type"] for s in payload["sections"]] == [SECTION_TYPE_THEME]
    assert section["claim_ids"] == [str(verified.alias_map[f"C{i}"]) for i in range(1, 5)]


# ---------------------------------------------------------------- fingerprint


_FIXED_COMPANY_ID = uuid4()
_FIXED_RESULT_ID = uuid4()


def _fingerprint_args(payload: dict, result_id: UUID, **overrides) -> dict:
    args = dict(
        outline_schema_version=REPORT_OUTLINE_SCHEMA_VERSION,
        synthesis_result_id=result_id,
        synthesis_result_fingerprint="d" * 64,
        company_id=_FIXED_COMPANY_ID,
        research_question_sha256="b" * 64,
        analysis_as_of=_AS_OF,
        outline_payload=payload,
    )
    args.update(overrides)
    return args


def test_outline_fingerprint_deterministic_sha256() -> None:
    payload = {"sections": [{"section_id": "S1", "section_type": "theme"}]}
    first = compute_outline_fingerprint(**_fingerprint_args(payload, _FIXED_RESULT_ID))
    second = compute_outline_fingerprint(**_fingerprint_args(payload, _FIXED_RESULT_ID))
    assert first == second
    assert len(first) == 64
    assert all(c in "0123456789abcdef" for c in first)


def test_outline_fingerprint_sensitive_to_derived_fields() -> None:
    # 指纹只由派生字段决定：result / payload / schema / company / question /
    # cutoff 任一变化 → 新指纹；outline_id / created_at 不在入参里，结构上无法
    # 影响指纹（排除保证）。
    base = _fingerprint_args({"sections": []}, uuid4())
    fp = compute_outline_fingerprint(**base)
    changed_payload = compute_outline_fingerprint(
        **{**base, "outline_payload": {"sections": [{"section_id": "S1"}]}}
    )
    assert changed_payload != fp
    changed_result_fp = compute_outline_fingerprint(
        **{**base, "synthesis_result_fingerprint": "e" * 64}
    )
    assert changed_result_fp != fp
    changed_result_id = compute_outline_fingerprint(**{**base, "synthesis_result_id": uuid4()})
    assert changed_result_id != fp
    changed_schema = compute_outline_fingerprint(**{**base, "outline_schema_version": 2})
    assert changed_schema != fp
    changed_company = compute_outline_fingerprint(**{**base, "company_id": uuid4()})
    assert changed_company != fp
    changed_cutoff = compute_outline_fingerprint(**{**base, "analysis_as_of": date(2026, 8, 11)})
    assert changed_cutoff != fp


# ---------------------------------------------------------------- parse_outline_sections


def _section(
    *,
    section_id: str = "S1",
    order: int = 1,
    section_type: str = SECTION_TYPE_THEME,
    title: str = "营收增长",
    claim_ids: list[str] | None = None,
    conflict_indexes: list[int] | None = None,
    evidence_gap_indexes: list[int] | None = None,
) -> dict:
    return {
        "section_id": section_id,
        "section_order": order,
        "section_type": section_type,
        "title": title,
        "claim_ids": claim_ids or [],
        "conflict_indexes": conflict_indexes or [],
        "evidence_gap_indexes": evidence_gap_indexes or [],
    }


def test_parse_outline_sections_valid() -> None:
    claim_ids = [str(uuid4()), str(uuid4())]
    payload = {
        "sections": [
            _section(claim_ids=claim_ids),
            _section(
                section_id="S2",
                order=2,
                section_type=SECTION_TYPE_RISKS_AND_GAPS,
                title=OUTLINE_RISKS_AND_GAPS_TITLE,
                conflict_indexes=[0],
                evidence_gap_indexes=[0, 1],
            ),
        ]
    }
    sections = parse_outline_sections(payload)

    assert len(sections) == 2
    assert sections[0].section_id == "S1"
    assert sections[0].section_type == SECTION_TYPE_THEME
    assert sections[0].claim_ids == tuple(UUID(cid) for cid in claim_ids)
    assert sections[0].conflict_indexes == ()
    assert sections[1].claim_ids == ()
    assert sections[1].conflict_indexes == (0,)
    assert sections[1].evidence_gap_indexes == (0, 1)


def test_parse_outline_sections_rejects_empty_sections() -> None:
    with pytest.raises(ReportOutlineIntegrityError):
        parse_outline_sections({"sections": []})


def test_parse_outline_sections_rejects_invalid_claim_id() -> None:
    payload = {"sections": [_section(claim_ids=["not-a-uuid"])]}
    with pytest.raises(ReportOutlineIntegrityError):
        parse_outline_sections(payload)


def test_parse_outline_sections_rejects_invalid_section() -> None:
    payload = {"sections": [{"section_id": "S1"}]}  # 缺 section_order/title 等
    with pytest.raises(ReportOutlineIntegrityError):
        parse_outline_sections(payload)
