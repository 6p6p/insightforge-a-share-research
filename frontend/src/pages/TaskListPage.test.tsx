import { describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';

import { renderWithProviders } from '../test/render';
import type { TaskResponse } from '../types/task';
import { TaskListPage } from './TaskListPage';

const mocks = vi.hoisted(() => ({
  listTasks: vi.fn(),
  deleteTask: vi.fn(),
}));

vi.mock('../api/tasks', () => ({
  taskKeys: {
    all: ['tasks'],
    list: () => ['tasks', 'list'],
    detail: (id: string) => ['tasks', 'detail', id],
    workspace: (id: string) => ['tasks', 'workspace', id],
  },
  listTasks: mocks.listTasks,
  deleteTask: mocks.deleteTask,
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
  mocks.deleteTask.mockReset();
  mocks.listTasks.mockResolvedValue({ items: [task()], total: 1, limit: 20, offset: 0 });
  mocks.deleteTask.mockResolvedValue(undefined);
});

describe('TaskListPage（v1.2.7-A：研究任务删除）', () => {
  it('渲染任务列表并显示删除按钮', async () => {
    renderWithProviders(<TaskListPage />);

    expect(await screen.findByText('三一重工')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '删除' })).toBeInTheDocument();
  });

  it('点击删除弹出确认弹窗（标题/文案/按钮）', async () => {
    renderWithProviders(<TaskListPage />);
    await screen.findByText('三一重工');

    fireEvent.click(screen.getByRole('button', { name: '删除' }));

    expect(await screen.findByText('删除研究任务')).toBeInTheDocument();
    expect(screen.getByText('确认删除该研究任务？删除后无法恢复。')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '确认删除' })).toBeInTheDocument();
  });

  it('确认删除调用 deleteTask 并刷新列表', async () => {
    renderWithProviders(<TaskListPage />);
    await screen.findByText('三一重工');

    fireEvent.click(screen.getByRole('button', { name: '删除' }));
    fireEvent.click(await screen.findByRole('button', { name: '确认删除' }));

    await waitFor(() => expect(mocks.deleteTask).toHaveBeenCalledWith('task-1'));
    // 刷新触发 listTasks 再次调用
    await waitFor(() => expect(mocks.listTasks.mock.calls.length).toBeGreaterThanOrEqual(2));
  });
});
