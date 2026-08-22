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

/** canonical public status（后端 task_status_projection 推导）：未开始/进行中/
 * 等待确认/已完成/失败/已取消。所有前端位置统一读取，不再各自推导。 */
export const PUBLIC_TASK_STATUS = [
  'not_started',
  'in_progress',
  'waiting_confirmation',
  'completed',
  'failed',
  'cancelled',
] as const;
export type PublicTaskStatus = (typeof PUBLIC_TASK_STATUS)[number];

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
  /** canonical public projection（五态，全部位置统一展示）。 */
  public_status: PublicTaskStatus;
  /** v1.2.6：带审核提醒完成信号（presentation only）——orchestration 以
   * completed_with_warnings 结束时为 true；只决定「已完成」vs
   * 「已完成（包含审核提醒）」的展示，真实状态保留在后端。 */
  completed_with_warnings?: boolean;
  created_at: string;
  updated_at: string;
}

export interface TaskListResponse {
  items: TaskResponse[];
  total: number;
  limit: number;
  offset: number;
}
