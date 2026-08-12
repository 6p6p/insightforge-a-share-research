/** 与后端 /app/schemas/source_record.py + /app/schemas/source_provider.py 对齐。

受控 Source Acquisition（7A Product Gate spec I）：上传 PDF / 受控 URL 导入
均复用后端既有 `POST /source-records/upload|import-url` 能力。
 */

export const SOURCE_DOCUMENT_TYPE = [
  'annual_report',
  'semiannual_report',
  'quarterly_report',
  'company_announcement',
  'issuer_ir_material',
  'prospectus',
  'news_article',
  'other',
] as const;
export type SourceDocumentType = (typeof SOURCE_DOCUMENT_TYPE)[number];

export interface SourceRecordResponse {
  source_id: string;
  company_id: string;
  provider_key: string;
  artifact_id: string;
  document_type: SourceDocumentType;
  title: string;
  published_at: string | null;
  reporting_period_end: string | null;
  source_url: string;
  acquisition_method: string;
  external_document_id: string | null;
  authority_tier_snapshot: number;
  critical_claim_eligible_snapshot: boolean;
  provider_capabilities_snapshot: string[];
  status: string;
  acquired_at: string;
  created_at: string;
  content_sha256: string;
  byte_size: number;
  media_type: string;
}

export interface SourceUrlImportRequest {
  company_id: string;
  provider_key: string;
  document_type: SourceDocumentType;
  title: string;
  source_url: string;
  published_at?: string | null;
  reporting_period_end?: string | null;
  external_document_id?: string | null;
}

export interface SourceProviderResponse {
  provider_key: string;
  display_name: string;
  provider_type: string;
  authority_tier: number;
  homepage_url: string;
  allowed_domains: string[];
  capabilities: string[];
  acquisition_methods: string[];
  exchange_scope: string[];
  requires_api_key: boolean;
  critical_claim_eligible: boolean;
  enabled: boolean;
}

export interface SourceProviderListResponse {
  items: SourceProviderResponse[];
  total: number;
}

/** 受控 URL 导入允许的文档类型（排除 news_article：后端 ingest 明确禁止）。 */
export const CONTROLLED_DOCUMENT_TYPES: readonly SourceDocumentType[] = [
  'annual_report',
  'semiannual_report',
  'quarterly_report',
  'company_announcement',
  'issuer_ir_material',
  'prospectus',
  'other',
];
