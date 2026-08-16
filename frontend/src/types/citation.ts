/** 引用导航只读契约（与后端 /app/schemas/citation.py 对齐，Stage 6B.2）。

ID 字段统一用 string（后端 UUID 序列化为字符串）。`provenance` 是
discriminated union（按 `origin_type`）：document_chunk / macro_observation。
`context_text` 只返回当前 Chunk 安全纯文本上下文（≤5000 chars），不返回整篇原文。
 */

/** 页级引用导航目标（spec O/P）：一次打开一个 evidence 或 claim citation。 */
export type CitationTarget =
  | { kind: 'evidence'; evidenceCardId: string }
  | { kind: 'claim'; claimId: string };

// ------------------------------------------------------------------ locator

export interface CitationLocator {
  locator_type: string;
  block_ordinal: number | null;
  char_start: number | null;
  char_end: number | null;
  /** html_dom 专属字段 */
  ordinal: number | null;
  tag: string | null;
  xpath: string | null;
  element_id: string | null;
  /** pdf_page 专属字段 */
  page_number: number | null;
  line_index: number | null;
  bbox: number[] | null;
  page_width: number | null;
  page_height: number | null;
}

// ------------------------------------------------------------------ document provenance

export interface DocumentProvenance {
  origin_type: 'document_chunk';
  source_id: string;
  provider_key: string;
  provider_label: string;
  title: string;
  source_url: string;
  published_at: string | null;
  authority_tier: number;
  document_type: string | null;
  raw_artifact_id: string;
  media_type: string;
  parsed_source_id: string;
  chunk_id: string;
  locator: CitationLocator | null;
  locator_refs: CitationLocator[];
  context_text: string;
  quote_text: string | null;
}

// ------------------------------------------------------------------ macro provenance

export interface MacroArtifactLink {
  role: string;
  page: number | null;
  artifact_id: string;
  media_type: string;
  fetched_at: string;
}

export interface MacroProvenance {
  origin_type: 'macro_observation';
  observation_id: string;
  period: string;
  value: string | null;
  is_missing: boolean;
  snapshot_id: string;
  fetched_at: string;
  series_id: string;
  indicator: string;
  geography: string;
  provider_key: string;
  provider_label: string;
  authority_tier: number;
  source_name: string | null;
  source_organization: string | null;
  raw_artifact_id: string | null;
  media_type: string | null;
  artifact_links: MacroArtifactLink[];
}

// ------------------------------------------------------------------ financial extraction provenance

export interface FinancialExtractionProvenance {
  origin_type: 'financial_extraction';
  source_id: string;
  provider_key: string;
  provider_label: string;
  title: string;
  source_url: string;
  published_at: string | null;
  authority_tier: number;
  document_type: string | null;
  raw_artifact_id: string;
  media_type: string;
  parsed_source_id: string;
  block_id: string;
  locator: CitationLocator | null;
  locator_refs: CitationLocator[];
  context_text: string;
  quote_text: string | null;
}

export type EvidenceProvenance =
  | DocumentProvenance
  | FinancialExtractionProvenance
  | MacroProvenance;

// ------------------------------------------------------------------ evidence citation

export interface EvidenceCitationClaimRelation {
  claim_id: string;
  claim_statement: string;
  relation: string;
}

export interface EvidenceCitationPayload {
  evidence_card_id: string;
  statement: string;
  quote_text: string | null;
  evidence_type: string;
  origin_type: string;
}

export interface EvidenceCitationResponse {
  evidence: EvidenceCitationPayload;
  claim_relations: EvidenceCitationClaimRelation[];
  provenance: EvidenceProvenance;
}

// ------------------------------------------------------------------ claim citation

export interface ClaimCitationEvidenceRelation {
  evidence_card_id: string;
  evidence_statement: string;
  relation: string;
}

export interface ClaimCitationResponse {
  claim_id: string;
  statement: string;
  domain: string;
  kind: string;
  confidence: string;
  importance: string;
  evidence_relations: ClaimCitationEvidenceRelation[];
}
