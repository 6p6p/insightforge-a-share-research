/** 任务级只读 artifact workspace 契约（与后端 /app/schemas/artifact.py 对齐，Stage 6B.1）。

ID 字段统一用 string（后端 UUID 序列化为字符串）。分页信封统一
`{ items, total, limit, offset }`。
 */

// ------------------------------------------------------------------ sources

export interface SourceArtifactResponse {
  source_id: string | null;
  company_id: string | null;
  provider_key: string | null;
  document_type: string | null;
  title: string | null;
  published_at: string | null;
  reporting_period_end: string | null;
  source_url: string | null;
  status: string | null;
  created_at: string | null;
  /** stage 6B.1 dual-origin：document_chunk 有 source_id，macro_observation 的 source_id=NULL。 */
  source_identity: string;
  origin_type: string;
  source_type: string | null;
  label: string | null;
  fetched_at: string | null;
  authority_tier: number | null;
  locator_summary: string | null;
}

export interface SourceArtifactListResponse {
  items: SourceArtifactResponse[];
  total: number;
  limit: number;
  offset: number;
}

// ------------------------------------------------------------------ evidence

export interface ClaimEvidenceRelation {
  claim_id: string;
  relation: string;
}

export interface EvidenceArtifactResponse {
  evidence_card_id: string;
  source_id: string | null;
  company_id: string;
  evidence_statement: string;
  evidence_type: string;
  extractor_confidence: string;
  quote_text: string | null;
  origin_type: string;
  created_at: string;
  /** stage 6B.1：canonical synthesis 中引用该卡的 claim 及其关系。 */
  used_by_claim_ids: string[];
  claim_relations: ClaimEvidenceRelation[];
  /** macro 卡专用（origin_type=macro_observation 时非空）。 */
  macro_observation_id: string | null;
  macro_snapshot_id: string | null;
  macro_series_id: string | null;
}

export interface EvidenceArtifactListResponse {
  items: EvidenceArtifactResponse[];
  total: number;
  limit: number;
  offset: number;
}

// ------------------------------------------------------------------ analysis

export interface WorkItemSummary {
  item_id: string;
  analysis_type: string;
  evidence_card_ids: string[];
  additional_evidence_ids: string[];
  macro_driver_evidence_ids: string[];
  company_evidence_ids: string[];
  calculation_ids: string[];
  comparison_ids: string[];
  claim_ids: string[];
}

export interface ClaimArtifactResponse {
  claim_id: string;
  company_id: string;
  analysis_domain: string;
  claim_kind: string;
  statement: string;
  confidence: string;
  importance: string;
  evidence_card_ids: string[];
  analyst_name: string | null;
}

export interface SynthesisThemeArtifact {
  title: string;
  summary: string;
  claim_ids: string[];
}

export interface SynthesisConflictArtifact {
  claim_ids: string[];
  description: string;
  severity: string;
  resolution_direction: string;
}

export interface SynthesisEvidenceGapArtifact {
  description: string;
  claim_ids: string[];
  suggested_evidence: string | null;
  priority: string;
}

export interface AnalysisArtifactResponse {
  company_id: string | null;
  research_question: string | null;
  analysis_as_of: string | null;
  work_items: WorkItemSummary[];
  claims: ClaimArtifactResponse[];
  synthesis_id: string | null;
  /** stage 6B.1：canonical synthesis result（最新 Stage5 checkpoint；无 Stage5 时取最新 Stage4）。 */
  synthesis_result_id: string | null;
  synthesis_fingerprint: string | null;
  result_fingerprint: string | null;
  themes: SynthesisThemeArtifact[];
  conflicts: SynthesisConflictArtifact[];
  evidence_gaps: SynthesisEvidenceGapArtifact[];
  /** research backflow 的新 Synthesis 无匹配 Stage4 时为 false（work_items 恒空，绝不混用旧工作项）。 */
  work_items_available: boolean;
}

// ------------------------------------------------------------------ report

export interface ReportParagraphArtifact {
  paragraph_index: number;
  text: string;
  claim_ids: string[];
  evidence_card_ids: string[];
  conflict_indexes: number[];
  evidence_gap_indexes: number[];
}

export interface ReportSectionArtifact {
  /** outline 符号键（如 "S2"），与审核 issue 的 section_id 关联。 */
  section_id: string;
  draft_section_id: string | null;
  section_order: number;
  section_type: string;
  title: string;
  paragraphs: ReportParagraphArtifact[];
}

export interface ReportArtifactResponse {
  report_id: string | null;
  outline_id: string | null;
  company_id: string | null;
  research_question_sha256: string | null;
  analysis_as_of: string | null;
  report_schema_version: number | null;
  report_fingerprint: string | null;
  section_count: number | null;
  /** stage 6B.1：真实正文投影。 */
  sections: ReportSectionArtifact[];
}

// ------------------------------------------------------------------ reviews

export interface ReviewIssueArtifactResponse {
  review_issue_id: string;
  ordinal: number;
  issue_type: string;
  severity: string;
  section_id: string;
  paragraph_index: number | null;
  message: string;
  related_claim_ids: string[];
  related_evidence_card_ids: string[];
}

export interface CheckFindingArtifact {
  code: string;
  section_id: string | null;
  paragraph_index: number | null;
  related_claim_ids: string[];
  related_evidence_card_ids: string[];
}

export interface ReportCheckArtifact {
  check_result_id: string;
  status: string;
  findings: CheckFindingArtifact[];
}

export interface ReviewActionArtifact {
  review_action_id: string;
  action_type: string;
  target_section_ids: string[];
  issue_count: number;
}

export interface HumanReviewArtifact {
  human_request_id: string;
  decision: string | null;
  comment: string | null;
  comment_exists: boolean;
  decided_at: string | null;
}

export interface ResearchBackflowArtifact {
  research_request_id: string;
  fulfilled: boolean;
  fulfillment_id: string | null;
  new_synthesis_result_id: string | null;
}

export interface PendingHumanReviewArtifact {
  /** 无 audit 行时的真实人工复核等待（P0）：reason + 可选裁决。 */
  reason: string | null;
  decision: string | null;
  comment: string | null;
  decided_at: string | null;
}
export interface ReviewsArtifactResponse {
  audit_id: string | null;
  report_id: string | null;
  audit_status: string | null;
  recommended_route: string | null;
  issue_count: number;
  audit_fingerprint: string | null;
  issues: ReviewIssueArtifactResponse[];
  /** Deterministic Check / ReviewAction / Human Review / Research Backflow 独立层，缺失为 null。 */
  check: ReportCheckArtifact | null;
  review_action: ReviewActionArtifact | null;
  human_review: HumanReviewArtifact | null;
  research_backflow: ResearchBackflowArtifact | null;
  pending_human_review: PendingHumanReviewArtifact | null;
}
