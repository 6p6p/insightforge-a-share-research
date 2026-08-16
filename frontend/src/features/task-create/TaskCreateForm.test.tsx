import { describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import dayjs from 'dayjs';

import { renderWithProviders } from '../../test/render';
import { ApiError } from '../../types/api';
import { TaskCreateForm } from './TaskCreateForm';

// AUTO 模式默认分析窗口（近 3 年至今）。
const DEFAULT_START = dayjs().subtract(3, 'year').format('YYYY-MM-DD');
const DEFAULT_END = dayjs().format('YYYY-MM-DD');

const mocks = vi.hoisted(() => ({
  createTask: vi.fn(),
  createOrchestration: vi.fn(),
}));

vi.mock('../../api/tasks', () => ({
  createTask: mocks.createTask,
  taskKeys: {
    all: ['tasks'],
    list: () => ['tasks', 'list'],
    detail: (id: string) => ['tasks', 'detail', id],
    workspace: (id: string) => ['tasks', 'workspace', id],
  },
}));

vi.mock('../../api/orchestrations', () => ({
  createOrchestration: mocks.createOrchestration,
}));

function mockTask(taskId: string): object {
  return {
    task_id: taskId,
    company_query: '宁德时代',
    research_start_date: DEFAULT_START,
    research_end_date: DEFAULT_END,
    modules: ALL_MODULES,
    questions: ['近三年盈利能力发生了什么变化？'],
    include_relative_valuation: false,
    require_plan_approval: false,
    status: 'pending',
    current_stage: 'created',
    progress: 0,
    created_at: '2026-08-11T00:00:00Z',
    updated_at: '2026-08-11T00:00:00Z',
  };
}

const ALL_MODULES = ['company_profile', 'business', 'financial', 'events', 'macro', 'risk'];

async function fillRequiredFields(): Promise<void> {
  // AUTO 模式：只需输入公司名；模块默认全选、分析窗口默认近 3 年。
  await userEvent.type(screen.getByLabelText('公司名称 / 代码'), '宁德时代');
  await userEvent.type(
    screen.getByLabelText('核心研究问题（可选）'),
    '近三年盈利能力发生了什么变化？',
  );
}

beforeEach(() => {
  mocks.createTask.mockClear();
  mocks.createOrchestration.mockClear();
});

describe('TaskCreateForm（V1.1 两阶段产品语义）', () => {
  it('单核心研究问题 + 默认自动研究 → createTask + createOrchestration（同批）', async () => {
    mocks.createTask.mockResolvedValue(mockTask('task-1'));
    mocks.createOrchestration.mockResolvedValue({ orchestration_id: 'orch-1' });
    const onCreated = vi.fn();

    renderWithProviders(<TaskCreateForm onCreated={onCreated} />);
    await fillRequiredFields();

    await userEvent.click(screen.getByRole('button', { name: '创建并自动开始研究' }));

    await waitFor(() => expect(mocks.createTask).toHaveBeenCalled());
    // Enter 提交（日期输入确认）与按钮点击都是合法提交入口；断言存在完整参数的提交。
    expect(mocks.createTask).toHaveBeenCalledWith({
      company_query: '宁德时代',
      research_start_date: DEFAULT_START,
      research_end_date: DEFAULT_END,
      modules: ALL_MODULES,
      questions: ['近三年盈利能力发生了什么变化？'],
      require_plan_approval: false,
    });
    await waitFor(() => expect(mocks.createOrchestration).toHaveBeenCalledWith('task-1'));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith('task-1'));
  });

  it('不填核心研究问题（P0 Company Only）→ questions 为空数组，自动研究照常启动', async () => {
    mocks.createTask.mockResolvedValue(mockTask('task-p0'));
    mocks.createOrchestration.mockResolvedValue({ orchestration_id: 'orch-p0' });
    const onCreated = vi.fn();

    renderWithProviders(<TaskCreateForm onCreated={onCreated} />);
    await userEvent.type(screen.getByLabelText('公司名称 / 代码'), '宁德时代');

    await userEvent.click(screen.getByRole('button', { name: '创建并自动开始研究' }));

    await waitFor(() => expect(mocks.createTask).toHaveBeenCalled());
    expect(mocks.createTask).toHaveBeenCalledWith({
      company_query: '宁德时代',
      research_start_date: DEFAULT_START,
      research_end_date: DEFAULT_END,
      modules: ALL_MODULES,
      questions: [],
      require_plan_approval: false,
    });
    await waitFor(() => expect(mocks.createOrchestration).toHaveBeenCalledWith('task-p0'));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith('task-p0'));
  });

  it('关闭「自动开始研究」→ 仅创建任务，不启动编排', async () => {
    mocks.createTask.mockResolvedValue(mockTask('task-3'));
    const onCreated = vi.fn();

    renderWithProviders(<TaskCreateForm onCreated={onCreated} />);
    await userEvent.click(screen.getByRole('switch', { name: '是否自动开始研究' }));
    await fillRequiredFields();

    await userEvent.click(screen.getByRole('button', { name: '创建任务' }));

    await waitFor(() => expect(mocks.createTask).toHaveBeenCalled());
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith('task-3'));
    expect(mocks.createOrchestration).not.toHaveBeenCalled();
  });

  it('任务创建成功但自动研究启动失败 → 不误报「创建失败」，提供重试与查看任务', async () => {
    mocks.createTask.mockResolvedValue(mockTask('task-9'));
    mocks.createOrchestration.mockRejectedValue(
      new ApiError(404, 'company_identity_not_found', '未找到匹配的公司身份', 'req-1'),
    );
    const onCreated = vi.fn();

    renderWithProviders(<TaskCreateForm onCreated={onCreated} />);
    await fillRequiredFields();
    await userEvent.click(screen.getByRole('button', { name: '创建并自动开始研究' }));

    await waitFor(() => expect(mocks.createTask).toHaveBeenCalled());
    await waitFor(() => expect(mocks.createOrchestration).toHaveBeenCalled());
    // 两阶段语义：不出现「创建失败」，而是「任务已创建，但自动研究未启动」。
    expect(screen.queryByText('创建失败')).toBeNull();
    await screen.findByText('任务已创建，但自动研究未启动。');
    expect(screen.getByText('未找到匹配的公司身份')).toBeTruthy();
    expect(onCreated).not.toHaveBeenCalled();

    // 「查看任务」按钮存在（保留 task_id，不误报创建失败）。
    expect(screen.getByRole('button', { name: '查看任务' })).toBeTruthy();

    // 重新启动研究 → 再次调用 orchestration，不重复创建任务。
    mocks.createOrchestration.mockResolvedValue({ orchestration_id: 'orch-2' });
    await userEvent.click(screen.getByRole('button', { name: '重新启动研究' }));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith('task-9'));
    expect(mocks.createTask).toHaveBeenCalledWith({
      company_query: '宁德时代',
      research_start_date: DEFAULT_START,
      research_end_date: DEFAULT_END,
      modules: ALL_MODULES,
      questions: ['近三年盈利能力发生了什么变化？'],
      require_plan_approval: false,
    });
  });
});
