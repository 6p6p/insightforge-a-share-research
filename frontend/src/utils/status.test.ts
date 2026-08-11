import { describe, expect, it } from 'vitest';

import type { TaskStatus } from '../types/task';
import { WORKFLOW_RUN_STATUS } from '../types/workflow';
import {
  RUN_STATUS_LABEL,
  TASK_STATUS_LABEL,
  runStatusTone,
  stageLabel,
  taskStatusTone,
} from './status';

describe('status mapping（spec N：不把 DB 枚举直接显示给用户）', () => {
  it('run 状态全部映射为中文', () => {
    for (const status of WORKFLOW_RUN_STATUS) {
      const label = RUN_STATUS_LABEL[status];
      expect(label).toBeTruthy();
      expect(label).not.toBe(status);
    }
    expect(RUN_STATUS_LABEL.waiting_human).toBe('等待人工确认');
    expect(RUN_STATUS_LABEL.completed).toBe('已完成');
    expect(RUN_STATUS_LABEL.failed).toBe('失败');
    expect(RUN_STATUS_LABEL.cancelled).toBe('已取消');
  });

  it('task 状态全部映射为中文（含 retrying）', () => {
    const taskStatuses: TaskStatus[] = [
      'pending',
      'running',
      'waiting_human',
      'retrying',
      'completed',
      'failed',
      'cancelled',
    ];
    for (const status of taskStatuses) {
      expect(TASK_STATUS_LABEL[status]).toBeTruthy();
      expect(TASK_STATUS_LABEL[status]).not.toBe(status);
    }
    expect(TASK_STATUS_LABEL.waiting_human).toBe('等待人工确认');
  });

  it('run tone：waiting_human→warning，completed→success，failed/cancelled→error', () => {
    expect(runStatusTone('waiting_human')).toBe('warning');
    expect(runStatusTone('completed')).toBe('success');
    expect(runStatusTone('failed')).toBe('error');
    expect(runStatusTone('cancelled')).toBe('error');
    expect(runStatusTone('running')).toBe('processing');
  });

  it('task tone：waiting_human→warning，completed→success', () => {
    expect(taskStatusTone('waiting_human')).toBe('warning');
    expect(taskStatusTone('completed')).toBe('success');
  });

  it('stage 标签回退：未知值保留原值，空值显示占位', () => {
    expect(stageLabel('analyzing')).toBe('分析中');
    expect(stageLabel('writing')).toBe('报告撰写');
    expect(stageLabel('unknown_stage')).toBe('unknown_stage');
    expect(stageLabel(null)).toBe('—');
  });
});
