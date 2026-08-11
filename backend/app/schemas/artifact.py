"""Read-only task artifact workspace contracts (Stage 6B.1).

任务级 artifact 视图（sources / evidence / analysis / report / reviews）的 API
响应契约。ID 集合由 checkpoint 精确恢复；本文件只定义只读投影，不包含任何写
入契约。分页信封统一 `{items, total, limit, offset}`（与 TaskListResponse /
SourceRecordListResponse 一致）。
"""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class SourceArtifactResponse(BaseModel):
    """任务引用的 source record 投影（task-scoped，非 company 全集）。"""

    model_config = ConfigDict(from_attributes=True)

    source_id: UUID
    company_id: UUID
    provider_key: str
    document_type: str
    title: str
    published_at: datetime | None = None
    reporting_period_end: date | None = None
    source_url: str
    status: str
    created_at: datetime


class SourceArtifactListResponse(BaseModel):
    items: list[SourceArtifactResponse]
    total: int
    limit: int
    offset: int


class EvidenceArtifactResponse(BaseModel):
    """任务引用的 evidence card 投影（task-scoped）。

    `source_id` 可空：macro_observation 卡不绑定 source_record。
    """

    model_config = ConfigDict(from_attributes=True)

    evidence_card_id: UUID
    source_id: UUID | None = None
    company_id: UUID
    evidence_statement: str
    evidence_type: str
    extractor_confidence: str
    quote_text: str | None = None
    origin_type: str
    created_at: datetime


class EvidenceArtifactListResponse(BaseModel):
    items: list[EvidenceArtifactResponse]
    total: int
    limit: int
    offset: int


class WorkItemSummary(BaseModel):
    """Stage 4 work item 的 checkpoint 投影（含其产物 claim_ids）。"""

    item_id: str
    analysis_type: str
    evidence_card_ids: list[UUID] = []
    additional_evidence_ids: list[UUID] = []
    macro_driver_evidence_ids: list[UUID] = []
    company_evidence_ids: list[UUID] = []
    calculation_ids: list[UUID] = []
    comparison_ids: list[UUID] = []
    claim_ids: list[UUID] = []


class ClaimArtifactResponse(BaseModel):
    """任务 claim 投影（映射 VerifiedSynthesisClaim，证据已内联）。"""

    claim_id: UUID
    company_id: UUID
    analysis_domain: str
    claim_kind: str
    statement: str
    confidence: str
    importance: str
    evidence_card_ids: list[UUID] = []
    analyst_name: str | None = None


class AnalysisArtifactResponse(BaseModel):
    """任务的 Stage 4 分析视图：work items + claims + synthesis 摘要。"""

    company_id: UUID | None = None
    research_question: str | None = None
    analysis_as_of: date | None = None
    work_items: list[WorkItemSummary] = []
    claims: list[ClaimArtifactResponse] = []
    synthesis_id: UUID | None = None
    synthesis_fingerprint: str | None = None


class ReportArtifactResponse(BaseModel):
    """任务的最新报告投影（verify_report_integrity 的 read-side 投影）。

    无 artifact 时所有字段为 null（200 语义）。
    """

    report_id: UUID | None = None
    outline_id: UUID | None = None
    company_id: UUID | None = None
    research_question_sha256: str | None = None
    analysis_as_of: date | None = None
    report_schema_version: int | None = None
    report_fingerprint: str | None = None
    section_count: int | None = None


class ReviewIssueArtifactResponse(BaseModel):
    """一条 audit issue 投影（related_* 保持字符串 UUID 列表）。"""

    review_issue_id: UUID
    ordinal: int
    issue_type: str
    severity: str
    section_id: str
    paragraph_index: int | None = None
    message: str
    related_claim_ids: list[str] = []
    related_evidence_card_ids: list[str] = []


class ReviewsArtifactResponse(BaseModel):
    """任务的最新审核视图：audit 摘要 + issues。"""

    audit_id: UUID | None = None
    report_id: UUID | None = None
    audit_status: str | None = None
    recommended_route: str | None = None
    issue_count: int = 0
    audit_fingerprint: str | None = None
    issues: list[ReviewIssueArtifactResponse] = []
