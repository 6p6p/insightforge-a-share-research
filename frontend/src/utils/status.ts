/** UI 中文状态映射（spec N）。不要把 DB 枚举直接显示给用户。 */

import type { PublicTaskStatus, TaskStage, TaskStatus } from '../types/task';
import type { WorkflowRunStatus } from '../types/workflow';

/** canonical public 六态（Product Consistency）：所有位置统一展示，不再由各
 * 组件从 task.status / run.status / orchestration.status 分别推导。 */
export const PUBLIC_STATUS_LABEL: Record<PublicTaskStatus, string> = {
  not_started: '未开始',
  in_progress: '进行中',
  waiting_confirmation: '等待确认',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
};

export const TASK_STATUS_LABEL: Record<TaskStatus, string> = {
  pending: '待执行',
  running: '运行中',
  waiting_human: '等待人工确认',
  retrying: '重试中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
};

export const RUN_STATUS_LABEL: Record<WorkflowRunStatus, string> = {
  pending: '待执行',
  running: '运行中',
  waiting_human: '等待人工确认',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
};

export const TASK_STAGE_LABEL: Record<TaskStage, string> = {
  created: '已创建',
  planning: '计划制定',
  collecting: '数据收集',
  parsing: '文档解析',
  evidence_extraction: '证据抽取',
  analyzing: '分析中',
  synthesizing: '综合研判',
  writing: '报告撰写',
  checking: '报告核验',
  auditing: '审核中',
  exporting: '导出中',
};

/** workflow event 的 stage 值直接用 TASK_STAGE_LABEL 或回退原值。 */
export function stageLabel(stage: string | null): string {
  if (!stage) {
    return '—';
  }
  return TASK_STAGE_LABEL[stage as TaskStage] ?? stage;
}

/** Ant Design Badge/Tag 的语义色。 */
export type PresetStatus = 'default' | 'processing' | 'success' | 'error' | 'warning';

export function runStatusTone(status: WorkflowRunStatus): PresetStatus {
  switch (status) {
    case 'running':
    case 'pending':
      return 'processing';
    case 'completed':
      return 'success';
    case 'failed':
    case 'cancelled':
      return 'error';
    case 'waiting_human':
      return 'warning';
  }
}

export function taskStatusTone(status: TaskStatus): PresetStatus {
  switch (status) {
    case 'running':
    case 'pending':
    case 'retrying':
      return 'processing';
    case 'completed':
      return 'success';
    case 'failed':
    case 'cancelled':
      return 'error';
    case 'waiting_human':
      return 'warning';
  }
}

/**
 * canonical public 状态中文标签（v1.2.6 补充）。
 * completed + completedWithWarnings → 「已完成（包含审核提醒）」；
 * 其余状态与 PUBLIC_STATUS_LABEL 完全一致（真实状态保留，仅展示区分）。
 */
export function publicStatusText(
  status: PublicTaskStatus,
  completedWithWarnings?: boolean,
): string {
  if (status === 'completed' && completedWithWarnings) {
    return '已完成（包含审核提醒）';
  }
  return PUBLIC_STATUS_LABEL[status] ?? status;
}

/** canonical public 六态语义色。 */
export function publicStatusTone(status: PublicTaskStatus): PresetStatus {
  switch (status) {
    case 'in_progress':
      return 'processing';
    case 'completed':
      return 'success';
    case 'failed':
      return 'error';
    case 'waiting_confirmation':
      return 'warning';
    case 'cancelled':
      return 'warning';
    case 'not_started':
      return 'default';
  }
}
