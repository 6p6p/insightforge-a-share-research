/** 与后端 /app/domain/tasks.py + /app/schemas/workflow.py 对齐。 */

export const WORKFLOW_RUN_STATUS = [
  'pending',
  'running',
  'waiting_human',
  'completed',
  'failed',
  'cancelled',
] as const;
export type WorkflowRunStatus = (typeof WORKFLOW_RUN_STATUS)[number];

export const WORKFLOW_EVENT_TYPE = [
  'run_created',
  'run_started',
  'node_completed',
  'run_completed',
  'run_failed',
  'run_waiting_human',
  'run_resumed',
  'run_cancelled',
] as const;
export type WorkflowEventType = (typeof WORKFLOW_EVENT_TYPE)[number];

/** Stage 1 simulation + Stage 5 真实研究的统一 human action。 */
export type ActionType =
  | 'approve_plan'
  | 'cancel'
  | 'retry'
  | 'approve'
  | 'rewrite'
  | 'research';

export interface WorkflowRunResponse {
  run_id: string;
  task_id: string;
  thread_id: string;
  graph_name: string;
  graph_version: string;
  status: WorkflowRunStatus;
  started_at: string | null;
  completed_at: string | null;
  failed_at: string | null;
  error_code: string | null;
  error_message: string | null;
  pending_action: string | null;
  created_at: string;
  updated_at: string;
}

export interface WorkflowActionRequest {
  action_type: ActionType;
  comment?: string | null;
}

export interface WorkflowActionResponse {
  run: WorkflowRunResponse;
  replayed: boolean;
}

export interface WorkflowEventResponse {
  event_id: number;
  run_id: string;
  event_type: WorkflowEventType;
  node_name: string | null;
  stage: string | null;
  progress: number | null;
  message: string;
  payload: Record<string, unknown>;
  created_at: string;
}
