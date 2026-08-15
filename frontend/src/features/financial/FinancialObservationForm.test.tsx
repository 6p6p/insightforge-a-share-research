import { describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { renderWithProviders } from '../../test/render';
import { ApiError } from '../../types/api';
import { FinancialObservationForm } from './FinancialObservationForm';

const mocks = vi.hoisted(() => ({
  createUserSuppliedFinancialObservation: vi.fn(),
}));

vi.mock('../../api/financial', () => ({
  createUserSuppliedFinancialObservation: mocks.createUserSuppliedFinancialObservation,
}));

async function setDate(label: string, value: string): Promise<void> {
  const input = screen.getByLabelText(label);
  await userEvent.clear(input);
  await userEvent.type(input, `${value}{enter}`);
  fireEvent.blur(input);
}

async function selectMetric(label: string): Promise<void> {
  await userEvent.click(screen.getByRole('combobox', { name: '指标' }));
  await screen.findByText(label);
  await userEvent.click(screen.getByText(label));
}

const mockResponse = {
  evidence_card_id: 'ev-1',
  source_id: 'src-1',
  metric_observation_id: 'mo-1',
  metric_fingerprint: 'fp-1',
  replayed: false,
};

describe('FinancialObservationForm（手动录入财务数据）', () => {
  beforeEach(() => {
    mocks.createUserSuppliedFinancialObservation.mockReset();
  });

  it('公司未解析 → 提示暂无法录入', () => {
    renderWithProviders(<FinancialObservationForm taskId="t1" companyId={null} />);
    expect(screen.getByText('公司尚未解析，暂无法录入财务数据')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '提交财务数据' })).toBeNull();
  });

  it('利润表指标提交 → 完整 payload + 成功提示并重置表单', async () => {
    mocks.createUserSuppliedFinancialObservation.mockResolvedValue(mockResponse);
    renderWithProviders(<FinancialObservationForm taskId="t1" companyId="c1" />);

    await selectMetric('营业收入');
    await setDate('期间开始日', '2023-01-01');
    await setDate('期间结束日', '2023-12-31');
    await userEvent.type(screen.getByLabelText('数值原文'), '4009.17');
    await userEvent.type(
      screen.getByLabelText('原文引文'),
      '报告期内，公司实现营业收入4009.17亿元。',
    );
    await userEvent.type(
      screen.getByLabelText('证据陈述'),
      '2023 年度公司实现营业收入 4009.17 亿元',
    );
    await userEvent.type(screen.getByLabelText('来源标题'), '宁德时代2023年年度报告');

    await userEvent.click(screen.getByRole('button', { name: '提交财务数据' }));

    await waitFor(() =>
      expect(mocks.createUserSuppliedFinancialObservation).toHaveBeenCalledTimes(1),
    );
    expect(mocks.createUserSuppliedFinancialObservation).toHaveBeenCalledWith('t1', {
      metric_code: 'revenue',
      statement_scope: 'consolidated',
      period_start: '2023-01-01',
      period_end: '2023-12-31',
      raw_unit: 'hundred_million_yuan',
      source_value_text: '4009.17',
      quote_text: '报告期内，公司实现营业收入4009.17亿元。',
      evidence_statement: '2023 年度公司实现营业收入 4009.17 亿元',
      source_title: '宁德时代2023年年度报告',
      source_url: null,
      document_type: 'annual_report',
    });
    expect(await screen.findByText('财务数据已登记（证据卡已创建）')).toBeInTheDocument();
  });

  it('资产负债表指标 → 隐藏期间开始日并提示；period_start 为 null', async () => {
    mocks.createUserSuppliedFinancialObservation.mockResolvedValue(mockResponse);
    renderWithProviders(<FinancialObservationForm taskId="t1" companyId="c1" />);

    await selectMetric('总资产');
    expect(screen.getByText('资产负债表指标为期末时点，仅需期间结束日')).toBeInTheDocument();
    expect(screen.queryByLabelText('期间开始日')).toBeNull();

    await setDate('期末日期', '2023-12-31');
    await userEvent.type(screen.getByLabelText('数值原文'), '8000');
    await userEvent.type(screen.getByLabelText('原文引文'), '公司总资产为8000亿元。');
    await userEvent.type(screen.getByLabelText('证据陈述'), '期末总资产 8000 亿元');
    await userEvent.type(screen.getByLabelText('来源标题'), '宁德时代2023年年度报告');

    await userEvent.click(screen.getByRole('button', { name: '提交财务数据' }));

    await waitFor(() =>
      expect(mocks.createUserSuppliedFinancialObservation).toHaveBeenCalledWith(
        't1',
        expect.objectContaining({
          metric_code: 'total_assets',
          period_start: null,
          period_end: '2023-12-31',
        }),
      ),
    );
  });

  it('422 校验失败 → 展示后端错误消息', async () => {
    mocks.createUserSuppliedFinancialObservation.mockRejectedValue(
      new ApiError(422, 'value_not_found_in_quote', '引文中未找到该数值', 'req-1'),
    );
    renderWithProviders(<FinancialObservationForm taskId="t1" companyId="c1" />);

    await selectMetric('营业收入');
    await setDate('期间开始日', '2023-01-01');
    await setDate('期间结束日', '2023-12-31');
    await userEvent.type(screen.getByLabelText('数值原文'), '4009.17');
    await userEvent.type(screen.getByLabelText('原文引文'), '引文中没有这个数字。');
    await userEvent.type(screen.getByLabelText('证据陈述'), 'x');
    await userEvent.type(screen.getByLabelText('来源标题'), '宁德时代2023年年度报告');

    await userEvent.click(screen.getByRole('button', { name: '提交财务数据' }));

    expect(await screen.findByText('引文中未找到该数值')).toBeInTheDocument();
    expect(screen.queryByText('财务数据已登记（证据卡已创建）')).toBeNull();
  });
});
