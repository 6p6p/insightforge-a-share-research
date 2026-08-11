/** Research task + workspace + execute 的 API（后端 /app/api/v1/routes/tasks.py）。 */

import { apiRequest } from './client';
import type { TaskCreateRequest, TaskListResponse, TaskResponse } from '../types/task';
import type {
  ResearchExecutionRequest,
  TaskWorkspaceResponse,
} from '../types/workspace';
import type { WorkflowRunResponse } from '../types/workflow';

/** TanStack Query 查询键（集中定义，便于 invalidation）。 */
export const taskKeys = {
  all: ['tasks'] as const,
  list: (params: { status?: string; limit: number; offset: number }) =>
    [...taskKeys.all, 'list', params] as const,
  detail: (taskId: string) => [...taskKeys.all, 'detail', taskId] as const,
  workspace: (taskId: string) => [...taskKeys.all, 'workspace', taskId] as const,
};

export async function createTask(payload: TaskCreateRequest): Promise<TaskResponse> {
  return apiRequest<TaskResponse>('/tasks', { method: 'POST', body: payload });
}

export async function listTasks(params: {
  status?: string;
  limit?: number;
  offset?: number;
} = {}): Promise<TaskListResponse> {
  const query = new URLSearchParams();
  if (params.status) {
    query.set('status', params.status);
  }
  query.set('limit', String(params.limit ?? 20));
  query.set('offset', String(params.offset ?? 0));
  const suffix = query.toString() ? `?${query.toString()}` : '';
  return apiRequest<TaskListResponse>(`/tasks${suffix}`);
}

export async function getTask(taskId: string): Promise<TaskResponse> {
  return apiRequest<TaskResponse>(`/tasks/${taskId}`);
}

export async function getTaskWorkspace(taskId: string): Promise<TaskWorkspaceResponse> {
  return apiRequest<TaskWorkspaceResponse>(`/tasks/${taskId}/workspace`);
}

/** 启动真实研究执行：显式 Stage 4 work plan。返回 Stage 4 run（202）。 */
export async function executeTask(
  taskId: string,
  payload: ResearchExecutionRequest,
): Promise<WorkflowRunResponse> {
  return apiRequest<WorkflowRunResponse>(`/tasks/${taskId}/execute`, {
    method: 'POST',
    body: payload,
  });
}
