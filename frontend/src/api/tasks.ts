/** Research task + workspace + execute 的 API（后端 /app/api/v1/routes/tasks.py）。 */

import { API_BASE_URL, apiRequest } from './client';
import type {
  AnalysisArtifactResponse,
  EvidenceArtifactListResponse,
  ReportArtifactResponse,
  ReviewsArtifactResponse,
  SourceArtifactListResponse,
} from '../types/artifacts';
import type {
  ClaimCitationResponse,
  EvidenceCitationResponse,
} from '../types/citation';
import type { ExportCreateResponse, ExportFormat } from '../types/export';
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
  sources: (taskId: string, params: { limit: number; offset: number }) =>
    [...taskKeys.all, 'artifacts', taskId, 'sources', params] as const,
  evidence: (taskId: string, params: { limit: number; offset: number }) =>
    [...taskKeys.all, 'artifacts', taskId, 'evidence', params] as const,
  analysis: (taskId: string) => [...taskKeys.all, 'artifacts', taskId, 'analysis'] as const,
  report: (taskId: string) => [...taskKeys.all, 'artifacts', taskId, 'report'] as const,
  reviews: (taskId: string) => [...taskKeys.all, 'artifacts', taskId, 'reviews'] as const,
  /** Stage 6B.2 citation navigation（task-scoped 只读）。 */
  citationEvidence: (taskId: string, evidenceCardId: string) =>
    [...taskKeys.all, 'citations', taskId, 'evidence', evidenceCardId] as const,
  citationClaim: (taskId: string, claimId: string) =>
    [...taskKeys.all, 'citations', taskId, 'claims', claimId] as const,
  /** Stage 6C export（任务级导出元数据；下载字节走 content 端点）。 */
  exports: (taskId: string) => [...taskKeys.all, 'exports', taskId] as const,
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

/**
 * v1.2.7-C: archive (soft delete). DELETE path stays compatible;
 * backend semantics changed to archive: downstream data is kept.
 */
export async function archiveTask(taskId: string): Promise<void> {
  await apiRequest<void>(`/tasks/${taskId}`, { method: 'DELETE' });
}

export async function getTaskWorkspace(taskId: string): Promise<TaskWorkspaceResponse> {
  return apiRequest<TaskWorkspaceResponse>(`/tasks/${taskId}/workspace`);
}

/** 任务引用的 source 列表（任务级 scoped，分页）。 */
export async function getTaskSources(
  taskId: string,
  params: { limit?: number; offset?: number } = {},
): Promise<SourceArtifactListResponse> {
  const query = new URLSearchParams();
  query.set('limit', String(params.limit ?? 20));
  query.set('offset', String(params.offset ?? 0));
  return apiRequest<SourceArtifactListResponse>(`/tasks/${taskId}/sources?${query.toString()}`);
}

/** 任务引用的 evidence card 列表（任务级 scoped，分页）。 */
export async function getTaskEvidence(
  taskId: string,
  params: { limit?: number; offset?: number } = {},
): Promise<EvidenceArtifactListResponse> {
  const query = new URLSearchParams();
  query.set('limit', String(params.limit ?? 20));
  query.set('offset', String(params.offset ?? 0));
  return apiRequest<EvidenceArtifactListResponse>(`/tasks/${taskId}/evidence?${query.toString()}`);
}

/** 任务 Stage 4 分析视图：work items + claims + synthesis 摘要。 */
export async function getTaskAnalysis(taskId: string): Promise<AnalysisArtifactResponse> {
  return apiRequest<AnalysisArtifactResponse>(`/tasks/${taskId}/analysis`);
}

/** 任务最新报告投影（verify_report_integrity read-side）。 */
export async function getTaskReport(taskId: string): Promise<ReportArtifactResponse> {
  return apiRequest<ReportArtifactResponse>(`/tasks/${taskId}/report`);
}

/** 任务最新审核视图：audit 摘要 + issues。 */
export async function getTaskReviews(taskId: string): Promise<ReviewsArtifactResponse> {
  return apiRequest<ReviewsArtifactResponse>(`/tasks/${taskId}/reviews`);
}

/** Evidence citation（Stage 6B.2 spec K）：evidence 头部 + canonical Claim
 * relations + verified Document/Macro provenance。task-scoped。 */
export async function getEvidenceCitation(
  taskId: string,
  evidenceCardId: string,
): Promise<EvidenceCitationResponse> {
  return apiRequest<EvidenceCitationResponse>(
    `/tasks/${taskId}/citations/evidence/${evidenceCardId}`,
  );
}

/** Claim citation（Stage 6B.2 spec L）：只允许 canonical synthesis input claim，
 * 返回 claim 元数据 + evidence relation list。task-scoped。 */
export async function getClaimCitation(
  taskId: string,
  claimId: string,
): Promise<ClaimCitationResponse> {
  return apiRequest<ClaimCitationResponse>(
    `/tasks/${taskId}/citations/claims/${claimId}`,
  );
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

// ------------------------------------------------------------------ Stage 6C export

/** 确定性报告导出：POST 创建（201）或 replay（200）。不可导出 → 409。 */
export async function createExport(
  taskId: string,
  format: ExportFormat,
): Promise<ExportCreateResponse> {
  return apiRequest<ExportCreateResponse>(`/tasks/${taskId}/export`, {
    method: 'POST',
    body: { format },
  });
}

/** 下载导出字节（content 端点）：返回 Blob + Content-Disposition 文件名。 */
export async function downloadExportContent(
  taskId: string,
  exportId: string,
): Promise<{ blob: Blob; fileName: string }> {
  const response = await fetch(
    `${API_BASE_URL}/tasks/${taskId}/exports/${exportId}/content`,
    { headers: { Accept: 'application/octet-stream' } },
  );
  if (!response.ok) {
    const text = await response.text();
    let message = `导出下载失败（HTTP ${response.status}）`;
    try {
      const envelope = JSON.parse(text) as { error?: { message?: string } };
      if (envelope?.error?.message) {
        message = envelope.error.message;
      }
    } catch {
      // 非 JSON 响应体：保留默认消息。
    }
    throw new Error(message);
  }
  const blob = await response.blob();
  const disposition = response.headers.get('Content-Disposition') ?? '';
  const match = /filename="?([^";]+)"?/.exec(disposition);
  const fileName = match?.[1] ?? `report_${exportId}`;
  return { blob, fileName };
}
