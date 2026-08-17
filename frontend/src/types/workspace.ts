/** 与后端 /app/schemas/research_execution.py + /app/schemas/company.py 对齐。 */

import type { TaskResponse } from './task';
import type { WorkflowRunResponse } from './workflow';

export interface CompanyIdentityResponse {
  company_id: string;
  /** 后端真实字段（无 company_name 字段；公司名用 short_name || official_name）。 */
  official_name: string;
  short_name: string;
  security_code: string | null;
  exchange: string | null;
  listing_status: string | null;
}

export interface ArtifactSummary {
  source_count: number;
  evidence_count: number;
  claim_count: number;
  report_count: number;
  review_issue_count: number;
}

export interface TaskWorkspaceResponse {
  task: TaskResponse;
  resolved_company: CompanyIdentityResponse | null;
  current_run: WorkflowRunResponse | null;
  artifact_summary: ArtifactSummary;
  /** 后台研究链（Stage4→Stage5 过渡）是否仍在执行：true 时即使 current_run
   * 已是 terminal 也不能关闭 task 级 SSE（spec D）。 */
  research_chain_active: boolean;
}

/** Stage 4 work plan：显式 work item（discriminated union，与后端 stage4.contracts 对齐）。 */

export const ANALYSIS_TYPE = [
  'business',
  'event',
  'risk',
  'financial',
  'macro',
  'valuation',
] as const;
export type AnalysisType = (typeof ANALYSIS_TYPE)[number];

export interface EvidenceWorkItem {
  item_id: string;
  analysis_type: 'business' | 'event' | 'risk';
  evidence_card_ids: string[];
}

export interface FinancialWorkItem {
  item_id: string;
  analysis_type: 'financial';
  calculation_ids: string[];
  additional_evidence_ids: string[];
}

export interface MacroWorkItem {
  item_id: string;
  analysis_type: 'macro';
  macro_driver_evidence_ids: string[];
  company_evidence_ids: string[];
}

export interface ValuationWorkItem {
  item_id: string;
  analysis_type: 'valuation';
  comparison_ids: string[];
}

export type AnalysisWorkItem =
  | EvidenceWorkItem
  | FinancialWorkItem
  | MacroWorkItem
  | ValuationWorkItem;

export interface ResearchExecutionRequest {
  analysis_work_items: AnalysisWorkItem[];
}
