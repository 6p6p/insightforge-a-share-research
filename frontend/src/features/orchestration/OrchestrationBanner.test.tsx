import { describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { renderWithProviders } from '../../test/render';
import type { ResearchOrchestrationResponse } from '../../types/orchestration';
import { OrchestrationBanner } from './OrchestrationBanner';

const mocks = vi.hoisted(() => ({
  actOnOrchestration: vi.fn(),
  resumeSourceAcquisition: vi.fn(),
  listSourceProviders: vi.fn(),
  importUrlSource: vi.fn(),
  uploadSourceFile: vi.fn(),
}));

vi.mock('../../api/orchestrations', () => ({
  actOnOrchestration: mocks.actOnOrchestration,
  resumeSourceAcquisition: mocks.resumeSourceAcquisition,
  orchestrationKeys: {
    all: ['orchestrations'],
    current: (id: string) => ['orchestrations', 'current', id],
    detail: (id: string) => ['orchestrations', 'detail', id],
  },
}));

vi.mock('../../api/sources', () => ({
  listSourceProviders: mocks.listSourceProviders,
  importUrlSource: mocks.importUrlSource,
  uploadSourceFile: mocks.uploadSourceFile,
  sourceKeys: {
    all: ['sources'],
    providers: () => ['sources', 'providers'],
  },
}));

vi.mock('../../api/tasks', () => ({
  taskKeys: {
    all: ['tasks'],
    workspace: (id: string) => ['tasks', 'workspace', id],
  },
}));

const base: ResearchOrchestrationResponse = {
  orchestration_id: 'orch-1',
  task_id: 't1',
  research_plan_id: 'rp-1',
  status: 'waiting_human',
  current_phase: 'awaiting_stage5',
  attempt_no: 1,
  retry_of_orchestration_id: null,
  error_code: null,
  error_message: null,
  started_at: '2026-08-12T00:00:00Z',
  completed_at: null,
  created_at: '2026-08-12T00:00:00Z',
  replayed: false,
  current_child_run_id: 'run-1',
  backflow_round: 0,
  research_request_id: null,
  manual_reason: null,
  missing_need_codes: [],
  updated_at: '2026-08-12T00:00:00Z',
};

function withPhase(overrides: Partial<ResearchOrchestrationResponse>): ResearchOrchestrationResponse {
  return { ...base, ...overrides };
}

const sseProvider = {
  provider_key: 'sse',
  display_name: '上海证券交易所',
  provider_type: 'exchange',
  authority_tier: 1,
  homepage_url: 'https://www.sse.com.cn',
  allowed_domains: ['sse.com.cn'],
  capabilities: ['company_announcement', 'document_download'],
  acquisition_methods: ['official_web_page', 'official_file_download'],
  exchange_scope: ['SSE'],
  requires_api_key: false,
  critical_claim_eligible: true,
  enabled: true,
};

describe('OrchestrationBanner（7A Product Gate spec N）', () => {
  it('无编排 → 不渲染', () => {
    const { container } = renderWithProviders(
      <OrchestrationBanner orchestration={null} companyId="c1" />,
    );
    expect(container.firstChild).toBeNull();
  });

  it('completed → 成功提示', () => {
    renderWithProviders(
      <OrchestrationBanner
        orchestration={withPhase({ status: 'completed', current_phase: 'completed' })}
        companyId="c1"
      />,
    );
    expect(screen.getByText('自动研究已完成')).toBeInTheDocument();
  });

  it('运行中 phase → 显示阶段文案，不渲染人工操作', () => {
    renderWithProviders(
      <OrchestrationBanner
        orchestration={withPhase({ status: 'running', current_phase: 'stage4' })}
        companyId="c1"
      />,
    );
    expect(screen.getByText('自动研究进行中：Stage 4 分析')).toBeInTheDocument();
    expect(screen.queryByText('需要人工确认')).not.toBeInTheDocument();
    expect(screen.queryByText('研究资料不足')).not.toBeInTheDocument();
  });

  it('awaiting_stage5 → approve/rewrite/research/cancel dispatch 到 orchestration actions', async () => {
    mocks.actOnOrchestration.mockResolvedValue({ orchestration_id: 'orch-1' });
    renderWithProviders(
      <OrchestrationBanner
        orchestration={withPhase({ current_phase: 'awaiting_stage5' })}
        companyId="c1"
      />,
    );

    expect(screen.getByText('等待报告审核')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '要求重写' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '需要补充研究' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '取消执行' })).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText('审批意见'), '同意发布');
    await userEvent.click(screen.getByRole('button', { name: '批准通过' }));

    await waitFor(() =>
      expect(mocks.actOnOrchestration).toHaveBeenCalledWith('orch-1', 'approve', '同意发布'),
    );
  });

  it('waiting_manual → 展示 need codes + 原因；导入官方 URL 成功后「继续研究」→ resume', async () => {
    mocks.listSourceProviders.mockResolvedValue({ items: [sseProvider], total: 1 });
    mocks.importUrlSource.mockResolvedValue({ source_id: 'src-1' });
    mocks.resumeSourceAcquisition.mockResolvedValue({ orchestration_id: 'orch-1' });

    renderWithProviders(
      <OrchestrationBanner
        orchestration={withPhase({
          current_phase: 'waiting_manual',
          manual_reason: 'source_acquisition_required',
          missing_need_codes: ['annual_report_financial', 'audit_report'],
        })}
        companyId="c1"
      />,
    );

    expect(await screen.findByText('研究资料不足')).toBeInTheDocument();
    // need codes 渲染在「缺失需求代码：」同一 div 内 → 用正则子串匹配。
    expect(screen.getByText(/annual_report_financial、audit_report/)).toBeInTheDocument();

    // 切到「导入官方 URL」tab。
    fireEvent.click(screen.getByRole('tab', { name: '导入官方 URL' }));

    // 选择来源机构（antd Select）。
    await userEvent.click(screen.getByRole('combobox', { name: '来源机构' }));
    await screen.findByText('上海证券交易所（sse）');
    await userEvent.click(screen.getByText('上海证券交易所（sse）'));

    await userEvent.type(screen.getByLabelText('标题'), '贵州茅台 2025 年报');
    await userEvent.type(
      screen.getByLabelText('官方 PDF 链接（受控域名）'),
      'https://static.sse.com.cn/annual.pdf',
    );
    await userEvent.click(screen.getByRole('button', { name: '导入并保存' }));

    await waitFor(() =>
      expect(mocks.importUrlSource).toHaveBeenCalledWith({
        company_id: 'c1',
        provider_key: 'sse',
        document_type: 'annual_report',
        title: '贵州茅台 2025 年报',
        source_url: 'https://static.sse.com.cn/annual.pdf',
      }),
    );

    expect(await screen.findByText('官方 URL 已导入为研究来源')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: '继续研究' }));
    await waitFor(() => expect(mocks.resumeSourceAcquisition).toHaveBeenCalledWith('orch-1'));
  });

  it('research_backflow + source_acquisition_required → 同样显示补资料面板（K2 路径）', async () => {
    mocks.listSourceProviders.mockResolvedValue({ items: [], total: 0 });
    renderWithProviders(
      <OrchestrationBanner
        orchestration={withPhase({
          current_phase: 'research_backflow',
          manual_reason: 'source_acquisition_required',
          missing_need_codes: ['annual_report_financial'],
        })}
        companyId="c1"
      />,
    );
    expect(await screen.findByText('研究资料不足')).toBeInTheDocument();
  });

  it('research_backflow + limit_reached → 不显示补资料面板（不可绕过 MAX rounds）', () => {
    renderWithProviders(
      <OrchestrationBanner
        orchestration={withPhase({
          current_phase: 'research_backflow',
          manual_reason: 'research_backflow_limit_reached',
        })}
        companyId="c1"
      />,
    );
    expect(screen.getByText('研究已暂停，等待人工介入')).toBeInTheDocument();
    expect(screen.queryByText('研究资料不足')).not.toBeInTheDocument();
  });

  it('research_backflow + structured_data_refresh_required → 显示结构化缺口警告，不显示补资料面板/继续研究', () => {
    renderWithProviders(
      <OrchestrationBanner
        orchestration={withPhase({
          current_phase: 'research_backflow',
          manual_reason: 'structured_data_refresh_required',
          missing_need_codes: ['macro:gdp_growth'],
        })}
        companyId="c1"
      />,
    );
    expect(screen.getByText('需要结构化数据补充')).toBeInTheDocument();
    expect(
      screen.getByText(/当前自动补充研究仅支持文档资料，该缺口需要人工处理或后续结构化数据刷新能力。/),
    ).toBeInTheDocument();
    // 不提供「上传 PDF / 导入官方 URL / 继续研究」的补资料面板。
    expect(screen.queryByText('研究资料不足')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '继续研究' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '上传并保存' })).not.toBeInTheDocument();
  });
});
