/** SSE 连接构造（spec I api/events.ts）。返回可新建 EventSource 的 URL。 */

import { API_BASE_URL } from './client';

/** task 级 SSE 端点 URL（无 Last-Event-ID 时后端从 0 重放全量事件）。 */
export function taskEventSourceUrl(taskId: string): string {
  return `${API_BASE_URL}/tasks/${taskId}/events`;
}

/** run 级 SSE 端点 URL（后端 /workflow-runs/{run_id}/events）。 */
export function runEventSourceUrl(runId: string): string {
  return `${API_BASE_URL}/workflow-runs/${runId}/events`;
}
