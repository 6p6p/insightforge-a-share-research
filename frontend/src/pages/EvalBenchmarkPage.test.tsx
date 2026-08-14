import { describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClientProvider } from '@tanstack/react-query';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';

import { createTestQueryClient } from '../test/render';
import type { BenchmarkAttemptRecord, BenchmarkSummaryPayload } from '../api/evalBenchmark';
import { EvalBenchmarkPage } from './EvalBenchmarkPage';

const mocks = vi.hoisted(() => ({
  getEvalBenchmarkSummary: vi.fn(),
}));

vi.mock('../api/evalBenchmark', () => ({
  evalBenchmarkKeys: {
    all: ['eval-benchmark'],
    summary: (run: string) => ['eval-benchmark', 'summary', run],
  },
  getEvalBenchmarkSummary: mocks.getEvalBenchmarkSummary,
}));

function attempt(overrides: Partial<BenchmarkAttemptRecord> = {}): BenchmarkAttemptRecord {
  return {
    dataset_id: 'insightforge_a_share_benchmark',
    dataset_version: 1,
    as_of: '2025-08-01',
    case_id: 'moutai-business',
    variant_id: 'single_rag',
    attempt_no: 1,
    mode: 'fake',
    status: 'success',
    error_code: null,
    wall_latency_ms: 662,
    execution_id: 'e'.repeat(32),
    variant_output_fingerprint: 'f'.repeat(64),
    usage_components: ['eval_single_rag_answer'],
    usage_call_count: 1,
    total_tokens: 40,
    estimated_cost_usd: '0.0000274',
    citation_validity: { status: 'computed', value: '1.0', numerator: '1', denominator: '1' },
    citation_coverage: { status: 'computed', value: '1.0', numerator: '1', denominator: '1' },
    persisted: true,
    expected_fail_fast: false,
    notes: [],
    ...overrides,
  };
}

const payload: BenchmarkSummaryPayload = {
  dataset_id: 'insightforge_a_share_benchmark',
  dataset_version: 1,
  as_of: '2025-08-01',
  mode: 'fake',
  model: 'deepseek:deepseek-v4-flash',
  generated_at: '2026-08-14T00:00:00+00:00',
  attempts: [
    attempt(),
    attempt({
      case_id: 'moutai-full',
      variant_id: 'single_rag',
      status: 'failed',
      error_code: 'single_rag_input_not_supported',
      wall_latency_ms: 0,
      usage_call_count: 0,
      total_tokens: null,
      estimated_cost_usd: null,
      citation_validity: null,
      citation_coverage: null,
      persisted: false,
      expected_fail_fast: true,
      variant_output_fingerprint: null,
    }),
  ],
};

function renderPage() {
  return render(
    <ConfigProvider locale={zhCN}>
      <QueryClientProvider client={createTestQueryClient()}>
        <EvalBenchmarkPage />
      </QueryClientProvider>
    </ConfigProvider>,
  );
}

describe('EvalBenchmarkPage', () => {
  it('渲染 attempt 表格与汇总统计', async () => {
    mocks.getEvalBenchmarkSummary.mockResolvedValue(payload);
    renderPage();
    await waitFor(() => {
      expect(screen.getByText('moutai-business')).toBeTruthy();
    });
    expect(screen.getAllByText('single_rag').length).toBeGreaterThan(0);
    expect(screen.getByText(/single_rag_input_not_supported/)).toBeTruthy();
    expect(screen.getByText('评估 Benchmark 对比')).toBeTruthy();
    // 汇总统计：2 attempts、1 成功、1 次调用。
    expect(screen.getAllByText('2').length).toBeGreaterThan(0);
    expect(screen.getAllByText('1').length).toBeGreaterThan(0);
  });

  it('切换 real 模式重新请求', async () => {
    mocks.getEvalBenchmarkSummary.mockResolvedValue(payload);
    renderPage();
    await waitFor(() => {
      expect(screen.getByText('moutai-business')).toBeTruthy();
    });
    await userEvent.click(screen.getByText('真实模型 (real)'));
    await waitFor(() => {
      expect(mocks.getEvalBenchmarkSummary).toHaveBeenCalledWith('real');
    });
  });

  it('读取失败显示警示', async () => {
    mocks.getEvalBenchmarkSummary.mockRejectedValue(new Error('benchmark run 结果不存在'));
    renderPage();
    await waitFor(() => {
      expect(screen.getByText('无法读取 benchmark 结果')).toBeTruthy();
    });
  });
});
