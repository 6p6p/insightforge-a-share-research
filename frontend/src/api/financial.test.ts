import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./client')>();
  return { ...actual, apiRequest: vi.fn() };
});

import { apiRequest } from './client';
import { createUserSuppliedFinancialObservation } from './financial';

const mockedApiRequest = vi.mocked(apiRequest);

describe('financial API（手动录入财务数据）', () => {
  beforeEach(() => {
    mockedApiRequest.mockReset();
  });

  it('createUserSuppliedFinancialObservation → POST /tasks/{task}/financial-observations', async () => {
    mockedApiRequest.mockResolvedValue({
      evidence_card_id: 'ev-1',
      source_id: 'src-1',
      metric_observation_id: 'mo-1',
      metric_fingerprint: 'fp-1',
      replayed: false,
    });
    await createUserSuppliedFinancialObservation('t1', {
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
    expect(mockedApiRequest).toHaveBeenCalledWith('/tasks/t1/financial-observations', {
      method: 'POST',
      body: {
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
      },
    });
  });

  it('资产负债表指标：period_start 为 null', async () => {
    mockedApiRequest.mockResolvedValue({
      evidence_card_id: 'ev-2',
      source_id: 'src-2',
      metric_observation_id: 'mo-2',
      metric_fingerprint: 'fp-2',
      replayed: false,
    });
    await createUserSuppliedFinancialObservation('t1', {
      metric_code: 'total_assets',
      statement_scope: 'consolidated',
      period_start: null,
      period_end: '2023-12-31',
      raw_unit: 'hundred_million_yuan',
      source_value_text: '8000',
      quote_text: '公司总资产为8000亿元。',
      evidence_statement: '期末总资产 8000 亿元',
      source_title: '宁德时代2023年年度报告',
      source_url: null,
      document_type: 'annual_report',
    });
    expect(mockedApiRequest).toHaveBeenCalledWith('/tasks/t1/financial-observations', {
      method: 'POST',
      body: expect.objectContaining({
        metric_code: 'total_assets',
        period_start: null,
        period_end: '2023-12-31',
      }),
    });
  });
});
