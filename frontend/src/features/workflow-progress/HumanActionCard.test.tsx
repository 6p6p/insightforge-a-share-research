import { describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { renderWithProviders } from '../../test/render';
import { ApiError } from '../../types/api';
import type { WorkflowRunResponse } from '../../types/workflow';
import { HumanActionCard } from './HumanActionCard';

const mocks = vi.hoisted(() => ({
  postRunAction: vi.fn(),
}));

vi.mock('../../api/workflow', () => ({
  postRunAction: mocks.postRunAction,
}));

function stage5Run(): WorkflowRunResponse {
  return {
    run_id: 'run-1',
    task_id: 'task-1',
    thread_id: 'thread-1',
    graph_name: 'stage5_report',
    graph_version: '1',
    status: 'waiting_human',
    started_at: null,
    completed_at: null,
    failed_at: null,
    error_code: null,
    error_message: null,
    pending_action: 'human_review',
    created_at: '2026-08-11T00:00:00Z',
    updated_at: '2026-08-11T00:00:00Z',
  };
}

describe('HumanActionCard（spec M）', () => {
  it('stage5 human_review 动态渲染 approve/rewrite/research/cancel 按钮', () => {
    renderWithProviders(<HumanActionCard run={stage5Run()} />);
    expect(screen.getByRole('button', { name: '批准通过' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '要求重写' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '需要补充研究' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '取消执行' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '批准计划' })).not.toBeInTheDocument();
  });

  it('点击批准 → postRunAction 收到 {action_type: approve, comment: null}', async () => {
    mocks.postRunAction.mockResolvedValue({ run: stage5Run(), replayed: false });
    renderWithProviders(<HumanActionCard run={stage5Run()} />);

    await userEvent.click(screen.getByRole('button', { name: '批准通过' }));

    await waitFor(() => expect(mocks.postRunAction).toHaveBeenCalledWith('run-1', { action_type: 'approve', comment: null }));
  });

  it('填写审批意见后点重写 → comment 转发', async () => {
    mocks.postRunAction.mockResolvedValue({ run: stage5Run(), replayed: false });
    renderWithProviders(<HumanActionCard run={stage5Run()} />);

    await userEvent.type(screen.getByLabelText('审批意见'), '细化估值假设');
    await userEvent.click(screen.getByRole('button', { name: '要求重写' }));

    await waitFor(() =>
      expect(mocks.postRunAction).toHaveBeenCalledWith('run-1', {
        action_type: 'rewrite',
        comment: '细化估值假设',
      }),
    );
  });

  it('409 → 显示状态变化告警并触发 onSettled，不假装成功', async () => {
    mocks.postRunAction.mockRejectedValue(
      new ApiError(409, 'active_workflow_run_exists', '该任务已存在进行中的工作流运行', 'req-1'),
    );
    const onSettled = vi.fn();
    renderWithProviders(<HumanActionCard run={stage5Run()} onSettled={onSettled} />);

    await userEvent.click(screen.getByRole('button', { name: '批准通过' }));

    expect(await screen.findByText('状态已变化')).toBeInTheDocument();
    await waitFor(() => expect(onSettled).toHaveBeenCalled());
  });
});
