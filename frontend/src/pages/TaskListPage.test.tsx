import { describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';

import { renderWithProviders } from '../test/render';
import type { TaskResponse } from '../types/task';
import { TaskListPage } from './TaskListPage';

const mocks = vi.hoisted(() => ({
  listTasks: vi.fn(),
  archiveTask: vi.fn(),
}));

vi.mock('../api/tasks', () => ({
  taskKeys: {
    all: ['tasks'],
    list: () => ['tasks', 'list'],
    detail: (id: string) => ['tasks', 'detail', id],
    workspace: (id: string) => ['tasks', 'workspace', id],
  },
  listTasks: mocks.listTasks,
  archiveTask: mocks.archiveTask,
}));

const task = (overrides: Partial<TaskResponse> = {}): TaskResponse => ({
  task_id: 'task-1',
  company_query: '三一重工',
  research_start_date: '2025-01-01',
  research_end_date: '2025-12-31',
  modules: ['financial'],
  questions: [],
  include_relative_valuation: false,
  require_plan_approval: true,
  status: 'completed',
  current_stage: 'exporting',
  progress: 100,
  public_status: 'completed',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  ...overrides,
});

beforeEach(() => {
  mocks.listTasks.mockReset();
  mocks.archiveTask.mockReset();
  mocks.listTasks.mockResolvedValue({ items: [task()], total: 1, limit: 20, offset: 0 });
  mocks.archiveTask.mockResolvedValue(undefined);
});

describe('TaskListPage（v1.2.7-C：研究任务归档）', () => {
  it('渲染任务列表并显示归档按钮', async () => {
    renderWithProviders(<TaskListPage />);

    expect(await screen.findByText('三一重工')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '归档' })).toBeInTheDocument();
  });

  it('点击归档弹出确认弹窗（标题/文案/按钮）', async () => {
    renderWithProviders(<TaskListPage />);
    await screen.findByText('三一重工');

    fireEvent.click(screen.getByRole('button', { name: '归档' }));

    expect(await screen.findByText('归档研究任务')).toBeInTheDocument();
    expect(screen.getByText('确认归档该研究任务？归档后任务将从任务列表隐藏，但研究数据会保留。')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '确认归档' })).toBeInTheDocument();
  });

  it('确认归档调用 archiveTask 并刷新列表', async () => {
    renderWithProviders(<TaskListPage />);
    await screen.findByText('三一重工');

    fireEvent.click(screen.getByRole('button', { name: '归档' }));
    fireEvent.click(await screen.findByRole('button', { name: '确认归档' }));

    await waitFor(() => expect(mocks.archiveTask).toHaveBeenCalledWith('task-1'));
    // 刷新触发 listTasks 再次调用
    await waitFor(() => expect(mocks.listTasks.mock.calls.length).toBeGreaterThanOrEqual(2));
  });
});
