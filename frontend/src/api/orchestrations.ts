/** 自动研究编排 API（后端 /app/api/v1/routes/research_orchestrations.py）。

一键入口 POST /tasks/{task_id}/orchestrations；补资料后
POST /research-orchestrations/{id}/resume-source-acquisition（同 thread 继续）；
Stage 5 人工决策经 POST /research-orchestrations/{id}/actions（继续顶层图）。
 */

import { apiRequest } from './client';
import type {
  OrchestrationAction,
  ResearchOrchestrationResponse,
} from '../types/orchestration';

/** TanStack Query 查询键（集中定义，便于 invalidation）。 */
export const orchestrationKeys = {
  all: ['orchestrations'] as const,
  current: (taskId: string) => [...orchestrationKeys.all, 'current', taskId] as const,
  detail: (orchestrationId: string) =>
    [...orchestrationKeys.all, 'detail', orchestrationId] as const,
};

/** 一键入口：为 task 启动自动研究编排（新建 201 / 已调度 202 / 已存在 200）。 */
export async function createOrchestration(
  taskId: string,
): Promise<ResearchOrchestrationResponse> {
  return apiRequest<ResearchOrchestrationResponse>(`/tasks/${taskId}/orchestrations`, {
    method: 'POST',
  });
}

/** 任务当前编排投影；尚无编排 → 404（调用方按需吞掉）。 */
export async function getCurrentOrchestration(
  taskId: string,
): Promise<ResearchOrchestrationResponse> {
  return apiRequest<ResearchOrchestrationResponse>(`/tasks/${taskId}/orchestrations/current`);
}

/** 补资料后继续：同 orchestration + 同顶层 thread，按 checkpoint 分类 resume。 */
export async function resumeSourceAcquisition(
  orchestrationId: string,
): Promise<ResearchOrchestrationResponse> {
  return apiRequest<ResearchOrchestrationResponse>(
    `/research-orchestrations/${orchestrationId}/resume-source-acquisition`,
    { method: 'POST' },
  );
}

/** Stage 5 人工决策（approve/rewrite/research/cancel）→ 驱动顶层编排继续。 */
export async function actOnOrchestration(
  orchestrationId: string,
  action: OrchestrationAction,
  comment?: string | null,
): Promise<ResearchOrchestrationResponse> {
  return apiRequest<ResearchOrchestrationResponse>(
    `/research-orchestrations/${orchestrationId}/actions`,
    { method: 'POST', body: { action, comment: comment ?? null } },
  );
}
