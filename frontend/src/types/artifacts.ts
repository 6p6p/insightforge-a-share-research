/** 任务级只读 artifact workspace 契约（与后端 /app/schemas/artifact.py 对齐，Stage 6B.1）。

ID 字段统一用 string（后端 UUID 序列化为字符串）。分页信封统一
`{ items, total, limit, offset }`。
 */

export interface SourceArtifactResponse {
  source_id: string;
  company_id: string;
  provider_key: string;
  document_type: string;
  title: string;
  published_at: string | null;
  reporting_period_end: string | null;
  source_url: string;
  status: string;
  created_at: string;
}

export interface SourceArtifactListResponse {
  items: SourceArtifactResponse[];
  total: number;
  limit: number;
  offset: number;
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
}

export interface EvidenceArtifactListResponse {
  items: EvidenceArtifactResponse[];
  total: number;
  limit: number;
  offset: number;
}

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

export interface AnalysisArtifactResponse {
  company_id: string | null;
  research_question: string | null;
  analysis_as_of: string | null;
  work_items: WorkItemSummary[];
  claims: ClaimArtifactResponse[];
  synthesis_id: string | null;
  synthesis_fingerprint: string | null;
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
}

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

export interface ReviewsArtifactResponse {
  audit_id: string | null;
  report_id: string | null;
  audit_status: string | null;
  recommended_route: string | null;
  issue_count: number;
  audit_fingerprint: string | null;
  issues: ReviewIssueArtifactResponse[];
}
