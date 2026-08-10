"""纯函数测试 helpers：构造 verified Outline / DraftSection / CheckInput（无 DB / 0 LLM）。

构造一组自洽的 `Scenario`：2 个 theme section（S1/S2）+ 1 个 risks_and_gaps
section（S3，conflict 0 + gap 0），4 个 Claims、2 张 Evidence 卡，每 section 一个
已验证 DraftSection。checks 域测试用它派生 `CheckInput`，再按需篡改 payload /
数据触发具体 finding。

S1 paragraph：营收 1500 亿元 + 净利率 50%（grounding：C1/C2/E1）。
S2 paragraph：产能利用率 85%（grounding：E2）。
S3 paragraph：引 conflict 0 + gap 0，无 evidence（risks/gaps policy 允许）。
"""

from dataclasses import dataclass
from datetime import date
from uuid import uuid4

from app.analysis.synthesis.contracts import (
    SynthesisAnalysisOutput,
    SynthesisClaimRole,
    SynthesisClaimRoleAssignment,
    SynthesisConflict,
    SynthesisEvidenceGap,
    SynthesisPriority,
    SynthesisSeverity,
    SynthesisTheme,
    VerifiedSynthesisResult,
)
from app.draft_section.contracts import VerifiedDraftSection
from app.report.checks import CheckInput, EvidenceCheckData
from app.report_outline.contracts import (
    OUTLINE_RISKS_AND_GAPS_TITLE,
    REPORT_OUTLINE_SCHEMA_VERSION,
    SECTION_TYPE_RISKS_AND_GAPS,
    SECTION_TYPE_THEME,
    OutlineSection,
    VerifiedReportOutline,
)

_QUESTION = "贵州茅台2026年营收与估值是否合理？"
_AS_OF = date(2026, 8, 10)

CLAIM_IDS = {f"C{i + 1}": uuid4() for i in range(4)}
EVIDENCE_IDS = {"E1": uuid4(), "E2": uuid4()}

CLAIM_STATEMENTS = {
    str(CLAIM_IDS["C1"]): "贵州茅台2026年预计营业收入约1500亿元。",
    str(CLAIM_IDS["C2"]): "贵州茅台2026年净利润率约50%。",
    str(CLAIM_IDS["C3"]): "公司产能利用率保持高位。",
    str(CLAIM_IDS["C4"]): "宏观利率环境维持稳定。",
}

EVIDENCE_DATA = {
    str(EVIDENCE_IDS["E1"]): EvidenceCheckData(
        evidence_card_id=EVIDENCE_IDS["E1"],
        evidence_statement="贵州茅台2026年营收目标1500亿元。",
        quote_text="营收目标1500亿元",
        origin_type="document_chunk",
        has_provenance=True,
        bound_claim_ids=(CLAIM_IDS["C1"],),
    ),
    str(EVIDENCE_IDS["E2"]): EvidenceCheckData(
        evidence_card_id=EVIDENCE_IDS["E2"],
        evidence_statement="公司产能利用率达85%。",
        quote_text="产能利用率85%",
        origin_type="document_chunk",
        has_provenance=True,
        bound_claim_ids=(CLAIM_IDS["C3"],),
    ),
}


def _synthesis_result() -> VerifiedSynthesisResult:
    alias_map = {ref: CLAIM_IDS[ref] for ref in CLAIM_IDS}
    output = SynthesisAnalysisOutput(
        summary="综合总结。",
        themes=[
            SynthesisTheme(title="营收增长", summary="A", claim_refs=["C1", "C2"]),
            SynthesisTheme(title="财务稳健", summary="B", claim_refs=["C3", "C4"]),
        ],
        claim_roles=[
            SynthesisClaimRoleAssignment(
                claim_ref=ref, role=SynthesisClaimRole.SUPPORT, rationale=f"支持 {ref}"
            )
            for ref in alias_map
        ],
        duplicates=[],
        conflicts=[
            SynthesisConflict(
                claim_refs=["C1", "C2"],
                description="营收口径存在分歧",
                severity=SynthesisSeverity.MEDIUM,
                resolution_direction="以年报披露为准",
            )
        ],
        evidence_gaps=[
            SynthesisEvidenceGap(
                description="缺少经营现金流证据",
                claim_refs=["C1"],
                suggested_evidence="经营现金流数据",
                priority=SynthesisPriority.MEDIUM,
            )
        ],
    )
    return VerifiedSynthesisResult(
        synthesis_result_id=uuid4(),
        synthesis_id=uuid4(),
        company_id=uuid4(),
        research_question=_QUESTION,
        research_question_sha256="b" * 64,
        analysis_as_of=_AS_OF,
        synthesis_fingerprint="c" * 64,
        result_fingerprint="d" * 64,
        input_claim_ids=tuple(CLAIM_IDS.values()),
        alias_map=alias_map,
        output=output,
    )


_SECTION_META = {
    "S1": (1, SECTION_TYPE_THEME, "营收增长"),
    "S2": (2, SECTION_TYPE_THEME, "财务稳健"),
    "S3": (3, SECTION_TYPE_RISKS_AND_GAPS, OUTLINE_RISKS_AND_GAPS_TITLE),
}


@dataclass
class Scenario:
    outline: VerifiedReportOutline
    drafts: dict[str, VerifiedDraftSection]  # section_id -> verified draft
    section_payloads: dict[str, dict]  # section_id -> draft section_payload
    claim_statements: dict[str, str]
    evidence: dict[str, EvidenceCheckData]

    def report_payload(self) -> dict:
        sections = []
        for outline_section in self.outline.sections:
            draft = self.drafts[outline_section.section_id]
            sections.append(
                {
                    "section_id": outline_section.section_id,
                    "section_order": outline_section.section_order,
                    "section_type": outline_section.section_type,
                    "title": outline_section.title,
                    "draft_section_id": str(draft.draft_section_id),
                    "paragraphs": self.section_payloads[outline_section.section_id]["paragraphs"],
                }
            )
        return {"sections": sections}

    def check_input(self, **overrides) -> CheckInput:
        payload = self.report_payload()
        input_kwargs = dict(
            verified_outline=self.outline,
            verified_drafts=dict(self.drafts),
            report_payload=payload,
            claim_statements=dict(self.claim_statements),
            evidence=dict(self.evidence),
        )
        input_kwargs.update(overrides)
        return CheckInput(**input_kwargs)


def _paragraph(
    *,
    text: str,
    claim_ids: list[str],
    evidence_ids: list[str] | None = None,
    conflict_indexes: list[int] | None = None,
    evidence_gap_indexes: list[int] | None = None,
) -> dict:
    return {
        "text": text,
        "claim_ids": list(claim_ids),
        "evidence_card_ids": list(evidence_ids or []),
        "conflict_indexes": list(conflict_indexes or []),
        "evidence_gap_indexes": list(evidence_gap_indexes or []),
    }


def make_scenario() -> Scenario:
    """构造自洽场景（每 section 一个已验证 draft + payload）。"""
    outline = _outline()
    drafts: dict[str, VerifiedDraftSection] = {}
    payloads: dict[str, dict] = {}
    for section_id in ("S1", "S2", "S3"):
        drafts[section_id] = _verified_draft(outline, section_id)
        payloads[section_id] = _section_payload(section_id)
    return Scenario(
        outline=outline,
        drafts=drafts,
        section_payloads=payloads,
        claim_statements=dict(CLAIM_STATEMENTS),
        evidence=dict(EVIDENCE_DATA),
    )


def _outline() -> VerifiedReportOutline:
    synthesis = _synthesis_result()
    sections = (
        OutlineSection(
            section_id="S1",
            section_order=1,
            section_type=SECTION_TYPE_THEME,
            title="营收增长",
            claim_ids=(CLAIM_IDS["C1"], CLAIM_IDS["C2"]),
            conflict_indexes=(),
            evidence_gap_indexes=(),
        ),
        OutlineSection(
            section_id="S2",
            section_order=2,
            section_type=SECTION_TYPE_THEME,
            title="财务稳健",
            claim_ids=(CLAIM_IDS["C3"], CLAIM_IDS["C4"]),
            conflict_indexes=(),
            evidence_gap_indexes=(),
        ),
        OutlineSection(
            section_id="S3",
            section_order=3,
            section_type=SECTION_TYPE_RISKS_AND_GAPS,
            title=OUTLINE_RISKS_AND_GAPS_TITLE,
            claim_ids=(),
            conflict_indexes=(0,),
            evidence_gap_indexes=(0,),
        ),
    )
    return VerifiedReportOutline(
        outline_id=uuid4(),
        synthesis_result_id=synthesis.synthesis_result_id,
        company_id=synthesis.company_id,
        research_question_sha256="b" * 64,
        analysis_as_of=_AS_OF,
        outline_schema_version=REPORT_OUTLINE_SCHEMA_VERSION,
        outline_fingerprint="c" * 64,
        sections=sections,
        verified_synthesis_result=synthesis,
    )


def _verified_draft(outline: VerifiedReportOutline, section_id: str) -> VerifiedDraftSection:
    order, section_type, title = _SECTION_META[section_id]
    return VerifiedDraftSection(
        draft_section_id=uuid4(),
        outline_id=outline.outline_id,
        section_id=section_id,
        section_order=order,
        section_type=section_type,
        title=title,
        section_schema_version=1,
        writer_name="evidence_bound_section_writer",
        writer_version=2,
        writer_model_id="deepseek:deepseek-v4-flash",
        writer_input_fingerprint="a" * 64,
        section_fingerprint="e" * 64,
        paragraph_count=1,
    )


def _section_payload(section_id: str) -> dict:
    if section_id == "S1":
        paragraphs = [
            _paragraph(
                text="贵州茅台2026年预计营业收入约1500亿元，净利润率约50%。",
                claim_ids=[str(CLAIM_IDS["C1"]), str(CLAIM_IDS["C2"])],
                evidence_ids=[str(EVIDENCE_IDS["E1"])],
            )
        ]
    elif section_id == "S2":
        paragraphs = [
            _paragraph(
                text="公司产能利用率达85%。",
                claim_ids=[str(CLAIM_IDS["C3"])],
                evidence_ids=[str(EVIDENCE_IDS["E2"])],
            )
        ]
    else:
        paragraphs = [
            _paragraph(
                text="关于营收口径存在分歧，且缺少经营现金流证据。",
                claim_ids=[str(CLAIM_IDS["C1"])],
                conflict_indexes=[0],
                evidence_gap_indexes=[0],
            )
        ]
    return {"paragraphs": paragraphs}
