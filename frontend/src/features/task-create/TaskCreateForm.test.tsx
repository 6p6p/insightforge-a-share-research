import { describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { renderWithProviders } from '../../test/render';
import { TaskCreateForm } from './TaskCreateForm';

const mocks = vi.hoisted(() => ({
  createTask: vi.fn(),
  executeTask: vi.fn(),
}));

vi.mock('../../api/tasks', () => ({
  createTask: mocks.createTask,
  executeTask: mocks.executeTask,
  taskKeys: {
    all: ['tasks'],
    list: () => ['tasks', 'list'],
    detail: (id: string) => ['tasks', 'detail', id],
    workspace: (id: string) => ['tasks', 'workspace', id],
  },
}));

async function setRangeDate(placeholder: string, value: string): Promise<void> {
  const input = screen.getByPlaceholderText(placeholder);
  await userEvent.clear(input);
  await userEvent.type(input, `${value}{enter}`);
  fireEvent.blur(input);
}

beforeEach(() => {
  mocks.createTask.mockClear();
  mocks.executeTask.mockClear();
});

describe('TaskCreateForm（spec J）', () => {
  it('填写基础字段后提交 → createTask 收到正确 payload 并回调 onCreated', async () => {
    mocks.createTask.mockResolvedValue({
      task_id: 'task-1',
      company_query: '贵州茅台',
      research_start_date: '2023-01-01',
      research_end_date: '2026-08-10',
      modules: ['financial'],
      questions: ['2026年营收是否合理？'],
      include_relative_valuation: false,
      require_plan_approval: true,
      status: 'pending',
      current_stage: 'created',
      progress: 0,
      created_at: '2026-08-11T00:00:00Z',
      updated_at: '2026-08-11T00:00:00Z',
    });
    const onCreated = vi.fn();

    renderWithProviders(<TaskCreateForm onCreated={onCreated} />);

    await userEvent.type(screen.getByLabelText('公司名称 / 代码'), '贵州茅台');

    // RangePicker（zhCN placeholder：开始日期 / 结束日期），输入后 Enter 提交。
    await setRangeDate('开始日期', '2023-01-01');
    await setRangeDate('结束日期', '2026-08-10');

    // 模块多选：打开下拉并选择「财务」。
    const combobox = screen.getByRole('combobox', { name: '研究模块' });
    await userEvent.click(combobox);
    await screen.findByText('财务');
    await userEvent.click(screen.getByText('财务'));

    await userEvent.type(screen.getByLabelText('研究问题（每行一个）'), '2026年营收是否合理？');

    await userEvent.click(screen.getByRole('button', { name: '创建任务' }));

    await waitFor(() => expect(mocks.createTask).toHaveBeenCalledTimes(1));
    expect(mocks.createTask).toHaveBeenCalledWith({
      company_query: '贵州茅台',
      research_start_date: '2023-01-01',
      research_end_date: '2026-08-10',
      modules: ['financial'],
      questions: ['2026年营收是否合理？'],
      include_relative_valuation: false,
      require_plan_approval: true,
    });
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith('task-1'));
    expect(mocks.executeTask).not.toHaveBeenCalled();
  });

  it('启用执行研究后提交 → 追加 executeTask 调用', async () => {
    mocks.createTask.mockResolvedValue({ task_id: 'task-2' });
    mocks.executeTask.mockResolvedValue({ run_id: 'run-1', status: 'running' });
    const onCreated = vi.fn();

    renderWithProviders(<TaskCreateForm onCreated={onCreated} />);

    // 打开「执行研究」Switch。
    await userEvent.click(screen.getByRole('switch', { name: '是否执行研究' }));

    // 切换到「财务」并填写计算 ID。
    await userEvent.click(screen.getByText('添加工作项'));
    await screen.findByText('工作项 1');
    await userEvent.click(screen.getByRole('combobox', { name: '工作项 1 分析类型' }));
    await screen.findByText('财务分析');
    await userEvent.click(screen.getByText('财务分析'));
    // ID 列表按整段值提交（等价于粘贴），逐字符 type 会被受控输入的
    // 规范化（splitIds/join 去掉尾部分隔符）打断，不适合此处。
    fireEvent.change(screen.getByLabelText('工作项 1 calculation_ids'), {
      target: { value: 'calc-1, calc-2' },
    });

    await userEvent.type(screen.getByLabelText('公司名称 / 代码'), '600519');
    await setRangeDate('开始日期', '2023-01-01');
    await setRangeDate('结束日期', '2026-08-10');
    const combobox = screen.getByRole('combobox', { name: '研究模块' });
    await userEvent.click(combobox);
    await screen.findByText('财务');
    await userEvent.click(screen.getByText('财务'));

    await userEvent.click(screen.getByRole('button', { name: '创建并执行研究' }));

    await waitFor(() => expect(mocks.createTask).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(mocks.executeTask).toHaveBeenCalledTimes(1));
    expect(mocks.executeTask).toHaveBeenCalledWith('task-2', {
      analysis_work_items: [
        {
          item_id: 'wi-1',
          analysis_type: 'financial',
          calculation_ids: ['calc-1', 'calc-2'],
          additional_evidence_ids: [],
        },
      ],
    });
  });
});
