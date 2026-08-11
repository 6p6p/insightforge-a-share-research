/** Workflow run 相关 API（后端 /app/api/v1/routes/workflows.py）。 */

import { apiRequest } from './client';
import type {
  WorkflowActionRequest,
  WorkflowActionResponse,
  WorkflowRunResponse,
} from '../types/workflow';

export async function getRun(runId: string): Promise<WorkflowRunResponse> {
  return apiRequest<WorkflowRunResponse>(`/workflow-runs/${runId}`);
}

/** 统一 human action 入口（approve_plan/cancel/retry + approve/rewrite/research/cancel）。 */
export async function postRunAction(
  runId: string,
  payload: WorkflowActionRequest,
): Promise<WorkflowActionResponse> {
  return apiRequest<WorkflowActionResponse>(`/workflow-runs/${runId}/actions`, {
    method: 'POST',
    body: payload,
  });
}
