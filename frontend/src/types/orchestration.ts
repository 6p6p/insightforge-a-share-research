/** 与后端 /app/schemas/research_orchestration.py 对齐（7A Product Gate）。

顶层自动研究编排（orchestration）的 status / phase 枚举与投影字段。
phase 枚举与后端 `ORCHESTRATION_PHASES` 一致；`awaiting_stage5` 等价于旧
Stage 5 waiting_human，UI 用现有 approve/rewrite/research/cancel。
 */

export const ORCHESTRATION_STATUS = [
  'pending',
  'running',
  'waiting_human',
  'completed',
  'completed_with_warnings',
  'failed',
  'cancelled',
] as const;
export type OrchestrationStatus = (typeof ORCHESTRATION_STATUS)[number];

export const ORCHESTRATION_PHASE = [
  'planning',
  'routing',
  'preparing',
  'fulfilling',
  'stage4',
  'stage5',
  'research_backflow',
  'waiting_manual',
  'awaiting_stage5',
  'completed',
] as const;
export type OrchestrationPhase = (typeof ORCHESTRATION_PHASE)[number];

/** orchestration human action（/research-orchestrations/{id}/actions 的 action 字面量）。 */
export const ORCHESTRATION_ACTION = ['approve', 'rewrite', 'research', 'cancel', 'retry'] as const;
export type OrchestrationAction = (typeof ORCHESTRATION_ACTION)[number];

export interface ResearchOrchestrationResponse {
  orchestration_id: string;
  task_id: string;
  research_plan_id: string | null;
  status: OrchestrationStatus;
  current_phase: OrchestrationPhase;
  attempt_no: number;
  retry_of_orchestration_id: string | null;
  error_code: string | null;
  error_message: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  replayed: boolean;
  current_child_run_id: string | null;
  backflow_round: number;
  research_request_id: string | null;
  /** 需要人工介入的原因（source_acquisition_required / structured_data_refresh_required /
   * research_backflow_limit_reached / research_backflow_no_progress）。 */
  manual_reason: string | null;
  /** 资料不足时缺失的研究需求代码（need_code）。 */
  missing_need_codes: string[];
  updated_at: string;
}

/** 需要补充资料后可 resume 的 manual_reason 集合（与后端 RESUME_BACKFLOW_MANUAL_REASONS 对齐；
 * 7A Product Gate spec D：structured_data_refresh_required 不在其中——结构化 refresh 不在
 * automatic 文档补充研究范围，补 PDF / URL 不能解决，后端 resume 以 InvalidAction 拒绝）。 */
export const RESUME_MANUAL_REASONS: readonly string[] = ['source_acquisition_required'];

/** 结构化数据补充缺口（7A.2B.3 scope 冻结）：不在 automatic 文档补充研究范围，
 * 上传 PDF / URL 不能解决 → 前端展示明确警告，不提供 resume-source-acquisition。 */
export const STRUCTURED_DATA_REFRESH_REASON = 'structured_data_refresh_required';

/** 回填达到上限：不能再绕过（后端 resume 会以 InvalidAction 拒绝）。 */
export const BACKFLOW_LIMIT_REASON = 'research_backflow_limit_reached';
/** P0 backflow manual closure request/decision projection（/backflow-review）。 */
export interface BackflowReview {
  orchestration_id: string;
  backflow_human_request_id: string | null;
  reason: string | null;
  decision: 'accept' | 'extra_research' | 'cancel' | null;
  comment: string | null;
  decided_at: string | null;
  /** accept 被确定性守卫拒绝时的中文理由（空 → 可接受）。 */
  acceptance_barriers: string[];
  /** v1.2.4 impact scope：report_blocking（暂不能接受）/ section_warning /
   * section_unavailable（带审核提醒可接受）/ info。 */
  impact_scope?: string | null;
}

/** backflow manual closure action（POST /backflow-review/actions）。 */
export type BackflowReviewAction = 'accept' | 'extra_research' | 'cancel';