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

/** 需要补充资料后可 resume 的 manual_reason 集合（与后端 RESUME_BACKFLOW_MANUAL_REASONS 对齐）。 */
export const RESUME_MANUAL_REASONS: readonly string[] = [
  'source_acquisition_required',
  'structured_data_refresh_required',
];

/** 回填达到上限：不能再绕过（后端 resume 会以 InvalidAction 拒绝）。 */
export const BACKFLOW_LIMIT_REASON = 'research_backflow_limit_reached';
