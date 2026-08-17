/** 中文状态标签（spec N）：run / task 状态统一走这里，不直接展示 DB 枚举。 */

import { Tag } from 'antd';
import type { PublicTaskStatus, TaskStatus } from '../types/task';
import type { WorkflowRunStatus } from '../types/workflow';
import {
  PUBLIC_STATUS_LABEL,
  RUN_STATUS_LABEL,
  TASK_STATUS_LABEL,
  publicStatusTone,
  runStatusTone,
  taskStatusTone,
} from '../utils/status';

type Tone = 'default' | 'processing' | 'success' | 'error' | 'warning';

const TONE_COLOR: Record<Tone, string> = {
  default: 'default',
  processing: 'processing',
  success: 'success',
  error: 'error',
  warning: 'warning',
};

interface Props {
  kind: 'run' | 'task' | 'public';
  status: WorkflowRunStatus | TaskStatus | PublicTaskStatus;
}

/** 从 label 表里按 kind 取中文（status 是联合类型，需索引签名兜底）。 */
function resolveLabel(kind: Props['kind'], status: Props['status']): string {
  if (kind === 'public') {
    return PUBLIC_STATUS_LABEL[status as PublicTaskStatus] ?? String(status);
  }
  if (kind === 'run') {
    return RUN_STATUS_LABEL[status as WorkflowRunStatus] ?? String(status);
  }
  return TASK_STATUS_LABEL[status as TaskStatus] ?? String(status);
}

function resolveTone(kind: Props['kind'], status: Props['status']): Tone {
  if (kind === 'public') {
    return publicStatusTone(status as PublicTaskStatus);
  }
  if (kind === 'run') {
    return runStatusTone(status as WorkflowRunStatus);
  }
  return taskStatusTone(status as TaskStatus);
}

export function StatusTag({ kind, status }: Props): React.JSX.Element {
  return <Tag color={TONE_COLOR[resolveTone(kind, status)]}>{resolveLabel(kind, status)}</Tag>;
}
