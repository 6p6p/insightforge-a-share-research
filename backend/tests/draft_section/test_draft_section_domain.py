"""DraftSection 领域纯函数单元测试（stage 5B, spec G/H/J/K/L/M/N/O/P）。

全部为确定性代码（**0 LLM / 0 DB**）：
- numeric token 提取 + grounding guard（spec L）；
- forbidden investment language（spec M）；
- C/E/X/G ref 格式（spec K）；
- fingerprint 确定性（spec O）；
- Section Input Pack 构造：alias 确定性、投影最小字段、LLM 永不看
  UUID / fingerprint（spec G/H）；
- validate_decision：known / cross-section / unbound / numeric / forbidden
  （spec K/L/M）；
- resolve_decision：persisted payload 只存真实 ID（spec N）；
- verify_resolved_payload：损坏 → 拒绝（spec P）。
"""

import json
from datetime import date
from uuid import UUID, uuid4

import pytest

from app.analysis.synthesis.contracts import (
    SynthesisAnalysisOutput,
    SynthesisClaimRole,
    SynthesisClaimRoleAssignment,
    SynthesisTheme,
    VerifiedSynthesisResult,
)
from app.draft_section.contracts import (
    ParagraphCandidate,
    WriterDecision,
    compute_section_fingerprint,
    compute_writer_input_fingerprint,
    contains_forbidden_language,
    valid_ref_format,
)
from app.draft_section.errors import (
    DraftSectionCrossSectionRef,
    DraftSectionForbiddenLanguage,
    DraftSectionIntegrityError,
    DraftSectionMalformedOutput,
    DraftSectionNumericGroundingError,
    DraftSectionUnboundEvidence,
    DraftSectionUnknownRef,
)
from app.draft_section.numeric import assert_numeric_grounding, extract_quantitative_tokens
from app.draft_section.packs import (
    LoadedClaim,
    LoadedEvidence,
    ResolvedConflict,
    ResolvedGap,
    SectionClaimItem,
    SectionEvidenceItem,
    SectionInputPack,
    build_section_input_pack,
)
from app.draft_section.prompt import (
    SECTION_PACK_END,
    SECTION_PACK_START,
    build_writer_messages,
)
from app.draft_section.validate import (
    resolve_decision,
    validate_decision,
    verify_resolved_payload,
)
from app.report_outline.contracts import (
    SECTION_TYPE_THEME,
    OutlineSection,
    VerifiedReportOutline,
)

_AS_OF = date(2026, 8, 10)

# 确定性 UUID：str 排序固定 → C1/C2、E1/E2 alias 分配确定（不依赖随机值）。
_U1 = UUID("00000000-0000-0000-0000-000000000001")
_U2 = UUID("00000000-0000-0000-0000-000000000002")
_E1 = UUID("00000000-0000-0000-0000-000000000011")
_E2 = UUID("00000000-0000-0000-0000-000000000012")


def _hex64(char: str = "a") -> str:
    return char * 64


# ---------------------------------------------------------------- helpers


def _claim(claim_id: UUID, statement: str, domain: str = "business") -> LoadedClaim:
    return LoadedClaim(
        claim_id=claim_id,
        claim_fingerprint=_hex64(str(claim_id)[-1]),
        statement=statement,
        analysis_domain=domain,
        claim_kind="inference",
        confidence="medium",
        importance="normal",
    )


def _evidence(
    evidence_card_id: UUID,
    statement: str,
    claim_ids: tuple[UUID, ...],
    *,
    quote: str | None = None,
) -> LoadedEvidence:
    return LoadedEvidence(
        evidence_card_id=evidence_card_id,
        evidence_fingerprint=_hex64(str(evidence_card_id)[-1]),
        evidence_statement=statement,
        evidence_type="fact",
        quote_text=quote,
        provider_key="xinhuanet",
        authority_tier=2,
        reporting_period_end=None,
        source_published_at=None,
        origin_type="document_chunk",
        relation="supports",
        claim_ids=tuple(sorted(claim_ids, key=str)),
    )


def _synthesis_output(claim_count: int = 3) -> SynthesisAnalysisOutput:
    refs = [f"C{i + 1}" for i in range(claim_count)]
    return SynthesisAnalysisOutput(
        summary="综合。",
        themes=[SynthesisTheme(title="主题", summary="摘要", claim_refs=refs)],
        claim_roles=[
            SynthesisClaimRoleAssignment(
                claim_ref=ref, role=SynthesisClaimRole.SUPPORT, rationale="r"
            )
            for ref in refs
        ],
        duplicates=[],
        conflicts=[],
        evidence_gaps=[],
    )


def _verified_outline(
    sections: list[OutlineSection],
    *,
    claim_ids: tuple[UUID, ...] = (),
) -> VerifiedReportOutline:
    verified_result = VerifiedSynthesisResult(
        synthesis_result_id=uuid4(),
        synthesis_id=uuid4(),
        company_id=uuid4(),
        research_question="研究问题",
        research_question_sha256=_hex64("b"),
        analysis_as_of=_AS_OF,
        synthesis_fingerprint=_hex64("c"),
        result_fingerprint=_hex64("d"),
        input_claim_ids=claim_ids,
        alias_map={f"C{i + 1}": cid for i, cid in enumerate(claim_ids)},
        output=_synthesis_output(len(claim_ids)),
    )
    return VerifiedReportOutline(
        outline_id=uuid4(),
        synthesis_result_id=verified_result.synthesis_result_id,
        company_id=verified_result.company_id,
        research_question_sha256=verified_result.research_question_sha256,
        analysis_as_of=_AS_OF,
        outline_schema_version=1,
        outline_fingerprint=_hex64("e"),
        sections=tuple(sections),
        verified_synthesis_result=verified_result,
    )


def _raw_section():
    """原始 Loaded 产物：C1=U1/C2=U2，E1（绑 C1）/E2（绑 C2），X1（C1）+G1（C1）。"""
    claims = [
        _claim(_U1, "公司营收同比增长15%。", "business"),
        _claim(_U2, "毛利率保持稳定。", "financial"),
    ]
    evidence = [
        _evidence(_E1, "2024年营收同比增长15%。", (_U1,), quote="营收同比增长15%"),
        _evidence(_E2, "毛利率约50%。", (_U2,)),
    ]
    conflicts = [
        ResolvedConflict(
            claim_ids=(_U1,),
            description="营收口径分歧",
            severity="medium",
            resolution_direction="以年报为准",
        )
    ]
    gaps = [
        ResolvedGap(
            claim_ids=(_U1,),
            description="缺现金流证据",
            suggested_evidence="现金流数据",
            priority="medium",
        )
    ]
    return claims, evidence, conflicts, gaps


def _make_pack() -> SectionInputPack:
    """C1/C2 两个 Claim + 各自绑定 E1/E2；X1（C1）+ G1（C1）。"""
    claims, evidence, conflicts, gaps = _raw_section()
    outline = _verified_outline(
        [
            OutlineSection(
                section_id="S1",
                section_order=1,
                section_type=SECTION_TYPE_THEME,
                title="主题",
                claim_ids=(_U1, _U2),
                conflict_indexes=(),
                evidence_gap_indexes=(),
            )
        ],
        claim_ids=(_U1, _U2),
    )
    return build_section_input_pack(
        outline=outline,
        section=outline.sections[0],
        company_name="贵州茅台",
        claims=claims,
        evidence=evidence,
        conflicts=conflicts,
        gaps=gaps,
    )


def _decision(paragraphs: list[ParagraphCandidate]) -> WriterDecision:
    return WriterDecision(paragraphs=paragraphs)


# ---------------------------------------------------------------- numeric (spec L)


def test_extract_quantitative_tokens() -> None:
    tokens = extract_quantitative_tokens(
        "2024年营收增长15%，同比增长0.5个百分点，达百分之二十。翻一番。"
    )
    assert "15%" in tokens
    assert "15" in tokens
    assert "2024" in tokens
    assert "翻一番" in tokens
    # 全角数字 / 全角百分号。
    tokens = extract_quantitative_tokens("２０２４年占比３０％")
    assert "２０２４" in tokens
    assert "３０％" in tokens
    # 中文数字（"百分之十五" → 连续中文字符"十五"）。
    tokens = extract_quantitative_tokens("同比约百分之十五")
    assert "十五" in tokens


def test_numeric_grounding_pass_and_fail() -> None:
    assert_numeric_grounding(
        paragraph_text="公司营收同比增长15%，2024年营收同比增长15%。",
        grounding_texts=["公司营收同比增长15%。", "2024年营收同比增长15%。"],
    )
    with pytest.raises(DraftSectionNumericGroundingError):
        assert_numeric_grounding(
            paragraph_text="公司营收同比增长15%，毛利率约99%。",
            grounding_texts=["公司营收同比增长15%。", "毛利率约50%。"],
        )


def test_extract_quantitative_tokens_ignores_inline_alias_refs() -> None:
    """内联 C/E/X/G 别名引用（C3/E1/X1/G1）的编号是标签，不是 quantitative token。"""
    tokens = extract_quantitative_tokens("（C3）引用，营收同比增长15%（E1），详见G1。")
    assert "3" not in tokens
    assert "1" not in tokens
    # 其余数字不受影响。
    assert "15%" in tokens
    assert "15" in tokens
    # 多位数 / 小写别名同样被剥离。
    tokens = extract_quantitative_tokens("（E12）与（c3）为证据引用，占比30%。")
    assert "12" not in tokens
    assert "3" not in tokens
    assert "30%" in tokens


def test_numeric_grounding_ignores_inline_alias_refs() -> None:
    """仅含别名编号、无其他数字的段落不触发 numeric grounding 拒绝。"""
    assert_numeric_grounding(
        paragraph_text="营收同比增长15%（C1，证据见E2）。",
        grounding_texts=["2024年营收同比增长15%。"],
    )


# ---------------------------------------------------------------- forbidden language (spec M)


def test_forbidden_investment_language() -> None:
    assert contains_forbidden_language("建议买入该股票") == "建议买入"
    assert contains_forbidden_language("目标价100元") == "目标价"
    assert contains_forbidden_language("收益承诺") == "收益承诺"
    assert contains_forbidden_language("公司营收保持增长态势。") is None


# ---------------------------------------------------------------- ref format (spec K)


def test_valid_ref_format() -> None:
    assert valid_ref_format("C1", "C")
    assert valid_ref_format("E12", "E")
    assert valid_ref_format("X3", "X")
    assert valid_ref_format("G9", "G")
    assert not valid_ref_format("C0", "C")
    assert not valid_ref_format("c1", "C")
    assert not valid_ref_format("C1a", "C")
    assert not valid_ref_format("", "C")
    assert not valid_ref_format(123, "C")


# ---------------------------------------------------------------- fingerprint (spec O)


def test_writer_input_fingerprint_determinism() -> None:
    kwargs = dict(
        section_schema_version=1,
        outline_fingerprint=_hex64("a"),
        section_id="S1",
        section_order=1,
        section_type="theme",
        title="主题",
        claim_fingerprints=[_hex64("1"), _hex64("2")],
        evidence_fingerprints=[_hex64("3")],
        conflicts=[],
        gaps=[],
        writer_name="evidence_bound_section_writer",
        writer_version=1,
        writer_model_id="deepseek:deepseek-v4-flash",
    )
    fp1 = compute_writer_input_fingerprint(**kwargs)
    fp2 = compute_writer_input_fingerprint(**kwargs)
    assert fp1 == fp2
    assert len(fp1) == 64
    # claim fingerprint 顺序不影响（sort_keys 规范化）。
    reordered = dict(kwargs, claim_fingerprints=[_hex64("2"), _hex64("1")])
    assert compute_writer_input_fingerprint(**reordered) == fp1
    # 任一输入变化 → 不同指纹。
    assert compute_writer_input_fingerprint(**dict(kwargs, section_id="S2")) != fp1
    assert (
        compute_writer_input_fingerprint(
            **dict(kwargs, writer_model_id="deepseek:deepseek-v4-flash@1")
        )
        != fp1
    )


def test_section_fingerprint_determinism() -> None:
    fp = compute_section_fingerprint(
        writer_input_fingerprint=_hex64("a"), section_payload={"paragraphs": [{"text": "x"}]}
    )
    assert len(fp) == 64
    assert (
        compute_section_fingerprint(
            writer_input_fingerprint=_hex64("a"), section_payload={"paragraphs": [{"text": "x"}]}
        )
        == fp
    )
    assert (
        compute_section_fingerprint(
            writer_input_fingerprint=_hex64("a"), section_payload={"paragraphs": [{"text": "y"}]}
        )
        != fp
    )


# ---------------------------------------------------------------- pack construction (spec G/H)


def test_pack_aliases_deterministic_and_projection_minimal() -> None:
    u1, u2 = uuid4(), uuid4()
    e1, e2 = uuid4(), uuid4()
    # 故意乱序传入：alias 必须按 str(claim_id) / str(evidence_card_id) 排序。
    claims = [_claim(u1, "a"), _claim(u2, "b")]
    evidence = [_evidence(e1, "x", (u2,)), _evidence(e2, "y", (u1,))]
    outline = _verified_outline(
        [
            OutlineSection(
                section_id="S1",
                section_order=1,
                section_type=SECTION_TYPE_THEME,
                title="主题",
                claim_ids=(u1, u2),
                conflict_indexes=(),
                evidence_gap_indexes=(),
            )
        ],
        claim_ids=(u1, u2),
    )
    pack = build_section_input_pack(
        outline=outline,
        section=outline.sections[0],
        company_name="公司",
        claims=claims,
        evidence=evidence,
        conflicts=[],
        gaps=[],
    )
    # C1 = 字典序较小的 claim_id，C2 = 较大的。
    ids = sorted((str(u1), str(u2)))
    assert pack.claim_alias_map() == {"C1": UUID(ids[0]), "C2": UUID(ids[1])}
    eids = sorted((str(e1), str(e2)))
    assert pack.evidence_alias_map() == {"E1": UUID(eids[0]), "E2": UUID(eids[1])}
    # 投影不含 fingerprint / provenance id。
    item: SectionClaimItem = pack.claims[0]
    assert not hasattr(item, "claim_fingerprint")
    ev_item: SectionEvidenceItem = pack.evidence[0]
    assert not hasattr(ev_item, "evidence_fingerprint")
    assert not hasattr(ev_item, "locator_refs")


def test_pack_evidence_binds_section_aliases_only() -> None:
    """Evidence 的 claim_aliases 只投影本 section 的 alias（过滤外部 claim）。"""
    u1, u2 = uuid4(), uuid4()
    e1 = uuid4()
    claims = [_claim(u1, "a"), _claim(u2, "b")]
    # E1 同时绑定 u1 与 u2（真实跨 claim 绑定）——本 section 两者都允许 → 都投影。
    evidence = [_evidence(e1, "x", (u1, u2))]
    outline = _verified_outline(
        [
            OutlineSection(
                section_id="S1",
                section_order=1,
                section_type=SECTION_TYPE_THEME,
                title="主题",
                claim_ids=(u1, u2),
                conflict_indexes=(),
                evidence_gap_indexes=(),
            )
        ],
        claim_ids=(u1, u2),
    )
    pack = build_section_input_pack(
        outline=outline,
        section=outline.sections[0],
        company_name="公司",
        claims=claims,
        evidence=evidence,
        conflicts=[],
        gaps=[],
    )
    assert pack.evidence[0].claim_aliases == tuple(sorted(("C1", "C2")))


def test_rendered_messages_have_no_uuid_or_fingerprint() -> None:
    pack = _make_pack()
    messages = build_writer_messages(pack)
    assert [m["role"] for m in messages] == ["system", "user"]
    system_content = messages[0]["content"]
    user_content = messages[1]["content"]
    # system prompt 冻结，不含任何 pack 内容。
    assert SECTION_PACK_START not in system_content
    assert "公司营收同比增长15%" not in system_content
    # Section Input Pack 只在 user（data delimiter 内）。
    assert SECTION_PACK_START in user_content
    assert SECTION_PACK_END in user_content
    payload = user_content[user_content.index(SECTION_PACK_START) :]
    assert "公司营收同比增长15%" in payload
    # 无 UUID / 64-hex fingerprint / provenance 字段名。
    import re

    uuid_re = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    assert re.search(uuid_re, payload) is None
    assert re.search(r"\b[0-9a-f]{64}\b", payload) is None
    for forbidden in ("claim_id", "evidence_card_id", "claim_fingerprint", "evidence_fingerprint"):
        assert forbidden not in payload


# ---------------------------------------------------------------- validate_decision (spec K/L/M)


def _paragraph(
    *,
    text: str = "公司营收同比增长15%，2024年营收同比增长15%。",
    claim_refs: list[str] | None = None,
    evidence_refs: list[str] | None = None,
    conflict_refs: list[str] | None = None,
    gap_refs: list[str] | None = None,
) -> ParagraphCandidate:
    return ParagraphCandidate(
        text=text,
        claim_refs=claim_refs or ["C1"],
        evidence_refs=evidence_refs or ["E1"],
        conflict_refs=conflict_refs or [],
        gap_refs=gap_refs or [],
    )


def test_validate_decision_valid_passes() -> None:
    pack = _make_pack()
    decision = _decision([_paragraph(text="公司营收同比增长15%，2024年营收同比增长15%。")])
    validate_decision(pack=pack, decision=decision, total_claim_count=2)


def test_validate_decision_unknown_claim_rejected() -> None:
    pack = _make_pack()
    # 编号超出合成输入集（total=2，C9 > 2）→ UnknownRef。
    decision = _decision([_paragraph(claim_refs=["C9"])])
    with pytest.raises(DraftSectionUnknownRef) as excinfo:
        validate_decision(pack=pack, decision=decision, total_claim_count=2)
    assert excinfo.value.code == "draft_section_unknown_ref"


def test_validate_decision_cross_section_claim_rejected() -> None:
    # section 只允许 C1（U1）；合成输入集 = {C1, C2, C3(U3)}。
    u3 = uuid4()
    claims, evidence, _, _ = _raw_section()
    outline = _verified_outline(
        [
            OutlineSection(
                section_id="S1",
                section_order=1,
                section_type=SECTION_TYPE_THEME,
                title="主题",
                claim_ids=(_U1,),
                conflict_indexes=(),
                evidence_gap_indexes=(),
            )
        ],
        claim_ids=(_U1, _U2, u3),
    )
    limited_pack = build_section_input_pack(
        outline=outline,
        section=outline.sections[0],
        company_name="公司",
        claims=[claims[0]],
        evidence=[evidence[0]],
        conflicts=[],
        gaps=[],
    )
    decision = _decision([_paragraph(claim_refs=["C1"], evidence_refs=["E1"])])
    # 合法：C1 属于本 section。
    validate_decision(pack=limited_pack, decision=decision, total_claim_count=3)
    # 引用 C2：在合成集（编号 2 <= 3）但不在本 section → CrossSection。
    decision2 = _decision([_paragraph(claim_refs=["C2"], evidence_refs=["E1"])])
    with pytest.raises(DraftSectionCrossSectionRef) as excinfo:
        validate_decision(pack=limited_pack, decision=decision2, total_claim_count=3)
    assert excinfo.value.code == "draft_section_cross_section_ref"


def test_validate_decision_unknown_evidence_rejected() -> None:
    pack = _make_pack()
    decision = _decision([_paragraph(evidence_refs=["E9"])])
    with pytest.raises(DraftSectionUnknownRef):
        validate_decision(pack=pack, decision=decision, total_claim_count=2)


def test_validate_decision_unbound_evidence_rejected() -> None:
    pack = _make_pack()
    # E2 只绑定 C2；段落引用 C1 + E2 → UnboundEvidence。
    decision = _decision([_paragraph(claim_refs=["C1"], evidence_refs=["E2"])])
    with pytest.raises(DraftSectionUnboundEvidence) as excinfo:
        validate_decision(pack=pack, decision=decision, total_claim_count=2)
    assert excinfo.value.code == "draft_section_unbound_evidence"


def test_validate_decision_numeric_hallucination_rejected() -> None:
    pack = _make_pack()
    decision = _decision([_paragraph(text="公司营收同比增长15%，毛利率约99%。")])
    with pytest.raises(DraftSectionNumericGroundingError) as excinfo:
        validate_decision(pack=pack, decision=decision, total_claim_count=2)
    assert excinfo.value.code == "draft_section_numeric_grounding_error"


def test_validate_decision_forbidden_language_rejected() -> None:
    pack = _make_pack()
    decision = _decision([_paragraph(text="公司营收同比增长15%，建议买入。")])
    with pytest.raises(DraftSectionForbiddenLanguage) as excinfo:
        validate_decision(pack=pack, decision=decision, total_claim_count=2)
    assert excinfo.value.code == "draft_section_forbidden_language"


def test_validate_decision_malformed_ref_format_rejected() -> None:
    pack = _make_pack()
    decision = _decision([_paragraph(claim_refs=["1C"])])
    with pytest.raises(DraftSectionMalformedOutput):
        validate_decision(pack=pack, decision=decision, total_claim_count=2)


# ---------------------------------------------------------------- resolve_decision (spec N)


def test_resolve_decision_payload_only_real_ids() -> None:
    pack = _make_pack()
    decision = _decision(
        [
            ParagraphCandidate(
                text="公司营收同比增长15%，2024年营收同比增长15%。",
                claim_refs=["C1"],
                evidence_refs=["E1"],
                conflict_refs=["X1"],
                gap_refs=["G1"],
            )
        ]
    )
    payload = resolve_decision(pack, decision)
    paragraph = payload["paragraphs"][0]
    assert paragraph["text"] == "公司营收同比增长15%，2024年营收同比增长15%。"
    assert paragraph["claim_ids"] == [str(_U1)]
    assert paragraph["evidence_card_ids"] == [str(_E1)]
    assert paragraph["conflict_indexes"] == [0]
    assert paragraph["evidence_gap_indexes"] == [0]
    blob = json.dumps(payload)
    # persisted payload 只存真实 ID：不含 alias / 内部字段名。
    assert "claim_refs" not in blob
    assert "evidence_refs" not in blob
    assert "conflict_refs" not in blob
    assert "gap_refs" not in blob
    assert "alias" not in blob


# ---------------------------------------------------------------- verify_resolved_payload (spec P)


def test_verify_resolved_payload_rejects_corruption() -> None:
    pack = _make_pack()
    good = resolve_decision(
        pack,
        _decision([_paragraph(text="公司营收同比增长15%，2024年营收同比增长15%。")]),
    )
    verify_resolved_payload(pack, good)  # 合法 payload 通过。

    # 段落结构损坏。
    with pytest.raises(DraftSectionIntegrityError):
        verify_resolved_payload(pack, {"paragraphs": []})
    # claim_id 不属于 allowed 集。
    corrupted = dict(good)
    corrupted["paragraphs"] = [dict(good["paragraphs"][0], claim_ids=[str(uuid4())])]
    with pytest.raises(DraftSectionIntegrityError):
        verify_resolved_payload(pack, corrupted)
    # evidence_card_id 不属于 allowed 集。
    corrupted = dict(good)
    corrupted["paragraphs"] = [dict(good["paragraphs"][0], evidence_card_ids=[str(uuid4())])]
    with pytest.raises(DraftSectionIntegrityError):
        verify_resolved_payload(pack, corrupted)
    # conflict index 越界。
    corrupted = dict(good)
    corrupted["paragraphs"] = [dict(good["paragraphs"][0], conflict_indexes=[9])]
    with pytest.raises(DraftSectionIntegrityError):
        verify_resolved_payload(pack, corrupted)
    # gap index 越界。
    corrupted = dict(good)
    corrupted["paragraphs"] = [dict(good["paragraphs"][0], evidence_gap_indexes=[9])]
    with pytest.raises(DraftSectionIntegrityError):
        verify_resolved_payload(pack, corrupted)
    # 缺 evidence_card_ids。
    corrupted = dict(good)
    corrupted["paragraphs"] = [dict(good["paragraphs"][0], evidence_card_ids=[])]
    with pytest.raises(DraftSectionIntegrityError):
        verify_resolved_payload(pack, corrupted)
