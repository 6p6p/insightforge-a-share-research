/** 与后端 /app/domain/tasks.py 的枚举对齐（值保持 DB/API 原样，UI 展示走 utils/status）。 */

export const TASK_STATUS = [
  'pending',
  'running',
  'waiting_human',
  'retrying',
  'completed',
  'failed',
  'cancelled',
] as const;
export type TaskStatus = (typeof TASK_STATUS)[number];

export const TASK_STAGE = [
  'created',
  'planning',
  'collecting',
  'parsing',
  'evidence_extraction',
  'analyzing',
  'synthesizing',
  'writing',
  'checking',
  'auditing',
  'exporting',
] as const;
export type TaskStage = (typeof TASK_STAGE)[number];

export const RESEARCH_MODULE = [
  'company_profile',
  'business',
  'financial',
  'events',
  'macro',
  'risk',
] as const;
export type ResearchModule = (typeof RESEARCH_MODULE)[number];

export interface TaskCreateRequest {
  company_query: string;
  research_start_date: string; // YYYY-MM-DD
  research_end_date: string; // YYYY-MM-DD
  modules: ResearchModule[];
  questions: string[];
  include_relative_valuation?: boolean;
  require_plan_approval?: boolean;
}

export interface TaskResponse {
  task_id: string;
  company_query: string;
  research_start_date: string;
  research_end_date: string;
  modules: ResearchModule[];
  questions: string[];
  include_relative_valuation: boolean;
  require_plan_approval: boolean;
  status: TaskStatus;
  current_stage: TaskStage;
  progress: number;
  created_at: string;
  updated_at: string;
}

export interface TaskListResponse {
  items: TaskResponse[];
  total: number;
  limit: number;
  offset: number;
}
