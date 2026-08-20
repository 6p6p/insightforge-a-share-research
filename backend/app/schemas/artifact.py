"""Read-only task artifact workspace contracts (Stage 6B.1).

任务级 artifact 视图（sources / evidence / analysis / report / reviews）的 API
响应契约。ID 集合由 checkpoint 精确恢复；本文件只定义只读投影，不包含任何写
入契约。分页信封统一 `{items, total, limit, offset}`（与 TaskListResponse /
SourceRecordListResponse 一致）。

stage 6B.1 spec 对齐：
- **dual-origin sources**：document_chunk（source_id 非空）与 macro_observation
  （source_id=NULL，source_identity 由 provider/series/snapshot 恢复）；
- **evidence relations**：used_by_claim_ids（必填）+ claim_relations（推荐），
  从 canonical synthesis run 的 claims + claim_evidence_links 派生；
- **analysis**：themes / conflicts / evidence_gaps 按真实 SynthesisResult v1
  contract 投影，alias refs（C1/E1/X1/G1）解析为稳定真实 claim IDs；
- **report**：真实 body——sections[].paragraphs[].{text, claim_ids,
  evidence_card_ids, conflict_indexes, evidence_gap_indexes}；
- **reviews**：Deterministic Check + Agent Audit + ReviewAction + Human Review +
  Research Backflow 分层；缺失 layer = null。
"""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

# ------------------------------------------------------------------ sources


class SourceArtifactResponse(BaseModel):
    """任务引用的 source 投影（dual-origin，task-scoped）。

    - document_chunk：source_id 非空，其余字段来自 source_records；
    - macro_observation：source_id=NULL，来源身份从 macro chain 恢复
      （provider → series → snapshot → observation），可被多个任务共享。
    """

    model_config = ConfigDict(from_attributes=True)

    source_id: UUID | None = None
    company_id: UUID | None = None
    provider_key: str | None = None
    document_type: str | None = None
    title: str | None = None
    published_at: datetime | None = None
    reporting_period_end: date | None = None
    source_url: str | None = None
    status: str | None = None
    created_at: datetime | None = None
    # stage 6B.1 dual-origin 投影字段。
    source_identity: str
    origin_type: str
    source_type: str | None = None
    label: str | None = None
    fetched_at: datetime | None = None
    authority_tier: int | None = None
    locator_summary: str | None = None


class SourceArtifactListResponse(BaseModel):
    items: list[SourceArtifactResponse]
    total: int
    limit: int
    offset: int


# ------------------------------------------------------------------ evidence


class ClaimEvidenceRelation(BaseModel):
    """一条 claim ↔ evidence 关系（supports / contradicts / context）。"""

    claim_id: UUID
    relation: str


class EvidenceArtifactResponse(BaseModel):
    """任务引用的 evidence card 投影（task-scoped）。

    `source_id` 可空：macro_observation 卡不绑定 source_record。stage 6B.1
    新增 `used_by_claim_ids`（必填）与 `claim_relations`（推荐）——canonical
    synthesis run 中引用该卡的全部 claim 及其关系。
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
    used_by_claim_ids: list[UUID] = []
    claim_relations: list[ClaimEvidenceRelation] = []
    # macro 卡专用（origin_type=macro_observation 时非空）。
    macro_observation_id: UUID | None = None
    macro_snapshot_id: UUID | None = None
    macro_series_id: UUID | None = None


class EvidenceArtifactListResponse(BaseModel):
    items: list[EvidenceArtifactResponse]
    total: int
    limit: int
    offset: int


# ------------------------------------------------------------------ analysis


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


class SynthesisThemeArtifact(BaseModel):
    """一个综合主题（alias refs 已解析为真实 claim_ids）。"""

    title: str
    summary: str
    claim_ids: list[UUID] = []


class SynthesisConflictArtifact(BaseModel):
    """一组冲突声明 + 冲突描述 / 严重度 / 解决方向（alias refs 已解析）。"""

    claim_ids: list[UUID] = []
    description: str
    severity: str
    resolution_direction: str


class SynthesisEvidenceGapArtifact(BaseModel):
    """一个证据缺口（alias refs 已解析为真实 claim_ids）。"""

    description: str
    claim_ids: list[UUID] = []
    suggested_evidence: str | None = None
    priority: str


class AnalysisArtifactResponse(BaseModel):
    """任务的 Stage 4 分析视图：work items + claims + synthesis 摘要 + 结构化综合。

    stage 6B.1 spec B/H：`synthesis_result_id` 是 **canonical synthesis**（最新
    Stage5 checkpoint 的 `synthesis_result_id`；无 Stage5 时取最新 Stage4）；
    `work_items` 只在匹配到同一 synthesis 的 Stage4 run 时暴露（research
    backflow 的新 Synthesis 无匹配 Stage4 → work_items=[] 且
    `work_items_available=false`，**绝不混用旧 S1 work items**）。
    """

    company_id: UUID | None = None
    research_question: str | None = None
    analysis_as_of: date | None = None
    work_items: list[WorkItemSummary] = []
    claims: list[ClaimArtifactResponse] = []
    synthesis_id: UUID | None = None
    synthesis_result_id: UUID | None = None
    synthesis_fingerprint: str | None = None
    result_fingerprint: str | None = None
    themes: list[SynthesisThemeArtifact] = []
    conflicts: list[SynthesisConflictArtifact] = []
    evidence_gaps: list[SynthesisEvidenceGapArtifact] = []
    work_items_available: bool = False


# ------------------------------------------------------------------ report


class ReportParagraphArtifact(BaseModel):
    """报告一个段落（verify_report_integrity 的 read-side 正文投影）。"""

    paragraph_index: int
    text: str
    claim_ids: list[UUID] = []
    evidence_card_ids: list[UUID] = []
    conflict_indexes: list[int] = []
    evidence_gap_indexes: list[int] = []


class ReportSectionArtifact(BaseModel):
    """报告一个 section（含段落正文）。

    `section_id` 是 outline 的**符号键**（如 "S2"），与 `ReviewIssueArtifactResponse.
    section_id` / CheckFinding 的 section 引用同一键，前端可据此关联审核→段落；
    `draft_section_id` 是 draft_sections 行的稳定 UUID（report payload 的
    `draft_section_id`，verify 后即真实产物 ID）。
    """

    section_id: str
    draft_section_id: UUID | None = None
    section_order: int
    section_type: str
    title: str
    paragraphs: list[ReportParagraphArtifact] = []


class ReportArtifactResponse(BaseModel):
    """任务的最新报告投影（verify_report_integrity 的 read-side 投影）。

    无 artifact 时所有字段为 null（200 语义）。stage 6B.1 spec I：真实 body
    以 `sections[].paragraphs[]` 返回（含 claim_ids / evidence_card_ids /
    conflict_indexes / evidence_gap_indexes），供前端正文渲染。
    """

    report_id: UUID | None = None
    outline_id: UUID | None = None
    company_id: UUID | None = None
    research_question_sha256: str | None = None
    analysis_as_of: date | None = None
    report_schema_version: int | None = None
    report_fingerprint: str | None = None
    section_count: int | None = None
    sections: list[ReportSectionArtifact] = []


# ------------------------------------------------------------------ reviews


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


class CheckFindingArtifact(BaseModel):
    """一条确定性 check 的 finding（不存长 prose，只存结构化字段）。"""

    code: str
    section_id: str | None = None
    paragraph_index: int | None = None
    related_claim_ids: list[str] = []
    related_evidence_card_ids: list[str] = []


class ReportCheckArtifact(BaseModel):
    """Deterministic Check 层投影（check_result_id + status + findings）。"""

    check_result_id: UUID
    status: str
    findings: list[CheckFindingArtifact] = []


class ReviewActionArtifact(BaseModel):
    """ReviewAction 层投影（action_type + target sections + issue count）。"""

    review_action_id: UUID
    action_type: str
    target_section_ids: list[str] = []
    issue_count: int = 0


class HumanReviewArtifact(BaseModel):
    """Human Review 层投影：request 是否存在 + decision / comment / decided_at。"""

    human_request_id: UUID
    decision: str | None = None
    comment: str | None = None
    comment_exists: bool = False
    decided_at: datetime | None = None


class ResearchBackflowArtifact(BaseModel):
    """Research Backflow 层投影：request 是否存在 + fulfillment / 新综合。"""

    research_request_id: UUID
    fulfilled: bool = False
    fulfillment_id: UUID | None = None
    new_synthesis_result_id: UUID | None = None


class PendingHumanReviewArtifact(BaseModel):
    """无 audit 行时的真实人工处理投影（P0/P2 一致性修复）。

    仅当 orchestration 处于 waiting_human 且 phase 属于人工复核（research_backflow /
    awaiting_stage5）而 stage5 checkpoint 尚无 audit_id 时填充：reason=该人工等待的
    真实原因（report_audit_unavailable / research_backfill_limit_reached 等）；
    decision/comment/decided_at=人工裁决（若有）。这一层反映真实后台状态，绝非伪造
    audit 行；Reviews 页据其显示「需要人工处理」而不误报「无审核记录」。
    """

    reason: str | None = None
    decision: str | None = None
    comment: str | None = None
    decided_at: datetime | None = None


class ReviewsArtifactResponse(BaseModel):
    """任务的最新审核视图（stage 6B.1 spec J 分层投影）。

    Agent Audit 摘要保留在顶层（audit_id / audit_status / recommended_route /
    issues）；Deterministic Check / ReviewAction / Human Review / Research
    Backflow 各自独立 layer，缺失为 null。所有层只读；绝不发送 prompt / raw
    provider response / reasoning_content。
    """

    audit_id: UUID | None = None
    report_id: UUID | None = None
    audit_status: str | None = None
    recommended_route: str | None = None
    issue_count: int = 0
    audit_fingerprint: str | None = None
    issues: list[ReviewIssueArtifactResponse] = []
    check: ReportCheckArtifact | None = None
    review_action: ReviewActionArtifact | None = None
    human_review: HumanReviewArtifact | None = None
    research_backflow: ResearchBackflowArtifact | None = None
    # P0/P2: 未生成 report_audit 行时的人工处理层（research_backflow manual closure /
    # awaiting_stage5 但 audit 尚未就绪）。只读投影真实人工等待状态，绝不伪造 audit。
    pending_human_review: PendingHumanReviewArtifact | None = None
