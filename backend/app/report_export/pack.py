"""Deterministic export pack builder (stage 6C spec I/J).

纯函数：不查 DB / 不调用 LLM。输入已验证 artifacts + provenance 映射 →
输出 `ExportReportPack`。

**引用编号（spec J）**：按 `section_order → paragraph_index → paragraph
evidence_card_ids（列表内顺序）` 首次出现分配 E1..En；同一 evidence 恒同号。
段落编号标记 = 该段 evidence_card_ids 逐项映射到的编号（顺序保留）；
renderer 只按 `citation_numbers` 在段落末尾追加 `[n]`，**绝不改写句子**。
"""

from dataclasses import dataclass
from uuid import UUID

from app.db.models.research_task import ResearchTaskModel
from app.report.contracts import VerifiedReport
from app.report_export.contracts import (
    EXPORT_SCHEMA_VERSION,
    ExportCitation,
    ExportParagraph,
    ExportReportPack,
    ExportSection,
)
from app.schemas.citation import (
    DocumentProvenance,
    FinancialExtractionProvenance,
    MacroProvenance,
)
from app.schemas.company import CompanyIdentityResponse

# 人工批准路径的 audit note（spec I：固定文案，不随 comment 变化）。
AUDIT_NOTE_HUMAN_APPROVED = "本报告存在经人工确认接受的审核冲突"
AUDIT_NOTE_BACKFLOW_ACCEPTED = "本报告经人工确认接受（补充研究已达上限，保留非关键审核提醒）"

# Provenance 判别常量（schemas/citation.py 的 origin_type）。
_ORIGIN_DOCUMENT = "document_chunk"
_ORIGIN_MACRO = "macro_observation"


@dataclass(frozen=True)
class ExportCardDetail:
    """导出的 evidence 卡投影（statement / quote / origin），不暴露 locator 明细。"""

    evidence_card_id: UUID
    statement: str
    quote_text: str | None
    origin_type: str


def build_export_report_pack(
    *,
    verified_report: VerifiedReport,
    task: ResearchTaskModel,
    company: CompanyIdentityResponse | None,
    cards_by_id: dict[UUID, ExportCardDetail],
    provenance_by_card: dict[UUID, DocumentProvenance | MacroProvenance],
    audit_note: str | None,
) -> ExportReportPack:
    """构建 ExportReportPack（引用编号 + 段落标记 + E1..En 附录 + 元数据）。

    `cards_by_id` / `provenance_by_card` 必须覆盖报告引用的全部 evidence 卡；
    缺卡 → `KeyError`（调用方 `ReportExportService` 负责先校验并转 integrity
    error，不静默降级）。
    """
    number_by_card: dict[UUID, int] = {}
    sections_out: list[ExportSection] = []
    for section in verified_report.report_payload.get("sections") or []:
        paragraphs_out: list[ExportParagraph] = []
        for paragraph in section.get("paragraphs") or []:
            numbers: list[int] = []
            for raw in paragraph.get("evidence_card_ids") or []:
                card_id = UUID(raw)
                if card_id not in number_by_card:
                    number_by_card[card_id] = len(number_by_card) + 1
                numbers.append(number_by_card[card_id])
            paragraphs_out.append(
                ExportParagraph(
                    text=paragraph.get("text", ""),
                    citation_numbers=tuple(numbers),
                )
            )
        sections_out.append(
            ExportSection(
                section_id=section.get("section_id", ""),
                title=section.get("title", ""),
                paragraphs=tuple(paragraphs_out),
            )
        )

    citations: list[ExportCitation] = []
    for card_id, number in sorted(number_by_card.items(), key=lambda pair: pair[1]):
        detail = cards_by_id[card_id]
        provenance = provenance_by_card[card_id]
        citations.append(_map_citation(number=number, detail=detail, provenance=provenance))

    return ExportReportPack(
        export_schema_version=EXPORT_SCHEMA_VERSION,
        task_id=task.task_id,
        report_id=verified_report.report_id,
        analysis_as_of=verified_report.analysis_as_of,
        research_start_date=task.research_start_date,
        research_end_date=task.research_end_date,
        company_name=_company_name(company, task),
        security_code=company.security_code if company is not None else None,
        research_question=_research_question(task),
        sections=tuple(sections_out),
        citations=tuple(citations),
        audit_note=audit_note,
        report_fingerprint=verified_report.report_fingerprint,
        check_result_id=None,
        check_fingerprint="",
        audit_id=None,
        audit_fingerprint="",
        human_decision_id=None,
        decision_fingerprint=None,
    )


def _company_name(company: CompanyIdentityResponse | None, task: ResearchTaskModel) -> str:
    if company is None:
        return task.company_query
    return company.short_name or company.official_name or task.company_query


def _research_question(task: ResearchTaskModel) -> str:
    questions = [str(q) for q in (task.questions or []) if str(q).strip()]
    if questions:
        return "；".join(questions)
    return task.company_query


def _map_citation(
    *,
    number: int,
    detail: ExportCardDetail,
    provenance: DocumentProvenance | MacroProvenance,
) -> ExportCitation:
    if isinstance(provenance, (DocumentProvenance, FinancialExtractionProvenance)):
        # document_chunk 与 financial_extraction 同构（quote / title / locator /
        # source 元数据）。xpath 属技术定位元数据，不进入 clean export projection。
        locator = provenance.locator
        page_number = None
        if locator is not None and locator.locator_type == "pdf_page":
            page_number = locator.page_number
        return ExportCitation(
            number=number,
            evidence_card_id=detail.evidence_card_id,
            statement=detail.statement,
            quote_text=detail.quote_text or provenance.quote_text,
            origin_type=detail.origin_type,
            provider_key=provenance.provider_key,
            provider_label=provenance.provider_label,
            title=provenance.title,
            published_at=provenance.published_at,
            fetched_at=None,
            source_url=provenance.source_url,
            page_number=page_number,
            xpath=None,
            indicator=None,
            geography=None,
            period=None,
        )
    return ExportCitation(
        number=number,
        evidence_card_id=detail.evidence_card_id,
        statement=detail.statement,
        quote_text=None,
        origin_type=detail.origin_type,
        provider_key=provenance.provider_key,
        provider_label=provenance.provider_label,
        title=provenance.indicator or provenance.source_name,
        published_at=None,
        fetched_at=provenance.fetched_at,
        source_url=None,
        page_number=None,
        xpath=None,
        indicator=provenance.indicator,
        geography=provenance.geography,
        period=provenance.period,
    )
