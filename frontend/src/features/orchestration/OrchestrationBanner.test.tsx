import { describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { renderWithProviders } from '../../test/render';
import { ApiError } from '../../types/api';
import type { ResearchOrchestrationResponse } from '../../types/orchestration';
import { OrchestrationBanner } from './OrchestrationBanner';

const mocks = vi.hoisted(() => ({
  actOnOrchestration: vi.fn(),
  actOnBackflowReview: vi.fn(),
  getBackflowReview: vi.fn(),
  resumeSourceAcquisition: vi.fn(),
  listSourceProviders: vi.fn(),
  importUrlSource: vi.fn(),
  uploadSourceFile: vi.fn(),
  resolveProvider: vi.fn(),
  getTaskWorkspace: vi.fn(),
  createUserSuppliedFinancialObservation: vi.fn(),
}));

vi.mock('../../api/financial', () => ({
  createUserSuppliedFinancialObservation: mocks.createUserSuppliedFinancialObservation,
}));

vi.mock('../../api/orchestrations', () => ({
  actOnOrchestration: mocks.actOnOrchestration,
  actOnBackflowReview: mocks.actOnBackflowReview,
  getBackflowReview: mocks.getBackflowReview,
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
  resolveProvider: mocks.resolveProvider,
  sourceKeys: {
    all: ['sources'],
    providers: () => ['sources', 'providers'],
  },
}));

vi.mock('../../api/tasks', () => ({
  getTaskWorkspace: mocks.getTaskWorkspace,
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

describe('OrchestrationBanner（V1.1 产品语义）', () => {
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
    expect(screen.getByText('研究完成')).toBeInTheDocument();
  });

  it('completed_with_warnings → 研究完成（包含审核提醒）', () => {
    renderWithProviders(
      <OrchestrationBanner
        orchestration={withPhase({ status: 'completed_with_warnings', current_phase: 'completed' })}
        companyId="c1"
      />,
    );
    expect(screen.getByText('研究完成')).toBeInTheDocument();
    expect(screen.getByText(/报告已生成（包含审核提醒）/)).toBeInTheDocument();
  });

  it('运行中 phase → 显示阶段文案，不渲染人工操作', () => {
    renderWithProviders(
      <OrchestrationBanner
        orchestration={withPhase({ status: 'running', current_phase: 'stage4' })}
        companyId="c1"
      />,
    );
    expect(screen.getByText('自动研究进行中：正在智能分析')).toBeInTheDocument();
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

    expect(screen.getByText('等待人工确认')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '要求重写' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '再次补充研究' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '取消研究' })).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText('审批意见'), '同意发布');
    await userEvent.click(screen.getByRole('button', { name: '接受报告' }));

    await waitFor(() =>
      expect(mocks.actOnOrchestration).toHaveBeenCalledWith('orch-1', 'approve', '同意发布'),
    );
  });

  it('waiting_manual → 产品化原因；need codes 折叠在技术详情；导入官方链接成功后「继续研究」→ resume', async () => {
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
    // 产品化原因：需要补充资料（不暴露 manual_reason 枚举）。
    expect(screen.getByText(/原因：需要补充资料/)).toBeInTheDocument();
    // need codes 折叠在「技术详情」内：产品术语为主文本 + 原始代码附注。
    fireEvent.click(screen.getByText('技术详情'));
    expect(
      screen.getByText(/缺失需求：年度报告、审计报告（原始代码：annual_report_financial、audit_report）/),
    ).toBeInTheDocument();

    // 切到「导入官方链接」tab。
    fireEvent.click(screen.getByRole('tab', { name: '导入官方链接' }));

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

    expect(await screen.findByText('官方链接已导入并开始处理')).toBeInTheDocument();
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

  it('research_backflow + limit_reached → 人工闭环卡片（接受/再次补充研究/取消）', async () => {
    mocks.getBackflowReview.mockResolvedValue({
      orchestration_id: 'orch-1',
      backflow_human_request_id: 'req-1',
      reason: 'research_backflow_limit_reached',
      decision: null,
      comment: null,
      decided_at: null,
      acceptance_barriers: [],
    });
    renderWithProviders(
      <OrchestrationBanner
        orchestration={withPhase({
          current_phase: 'research_backflow',
          manual_reason: 'research_backflow_limit_reached',
        })}
        companyId="c1"
      />,
    );
    expect(await screen.findByText('自动补充研究已达到上限')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '接受当前报告' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '再次补充研究' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '取消研究' })).toBeInTheDocument();
    expect(screen.queryByText('研究资料不足')).not.toBeInTheDocument();
  });

  it('research_backflow + limit_reached + 关键 barrier → 接受按钮禁用并给出中文理由', async () => {
    mocks.getBackflowReview.mockResolvedValue({
      orchestration_id: 'orch-1',
      backflow_human_request_id: 'req-1',
      reason: 'research_backflow_limit_reached',
      decision: null,
      comment: null,
      decided_at: null,
      acceptance_barriers: ['报告检查未通过', '存在关键完整性失败'],
    });
    renderWithProviders(
      <OrchestrationBanner
        orchestration={withPhase({
          current_phase: 'research_backflow',
          manual_reason: 'research_backflow_limit_reached',
        })}
        companyId="c1"
      />,
    );
    expect(await screen.findByText('当前报告暂不能接受')).toBeInTheDocument();
    expect(screen.getByText('报告检查未通过；存在关键完整性失败')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '接受当前报告' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '再次补充研究' })).toBeEnabled();
  });

  it('research_backflow + section_warning scope → 显示审核提醒且接受可用（v1.2.4）', async () => {
    mocks.getBackflowReview.mockResolvedValue({
      orchestration_id: 'orch-1',
      backflow_human_request_id: 'req-1',
      reason: 'research_backflow_limit_reached',
      decision: null,
      comment: null,
      decided_at: null,
      acceptance_barriers: [],
      impact_scope: 'section_warning',
    });
    renderWithProviders(
      <OrchestrationBanner
        orchestration={withPhase({
          current_phase: 'research_backflow',
          manual_reason: 'research_backflow_limit_reached',
        })}
        companyId="c1"
      />,
    );
    await screen.findByText('当前报告存在审核提醒');
    expect(screen.getByText(/部分章节存在缺口，但其他内容仍可查看与接受/)).toBeInTheDocument();
    expect(screen.queryByText('当前报告暂不能接受')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '接受当前报告' })).toBeEnabled();
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
    expect(screen.getByText('需要更新结构化数据')).toBeInTheDocument();
    expect(
      screen.getByText(/自动补充研究已尽力完成文档类资料缺口；该估值数据缺口不在自动补充研究范围内/),
    ).toBeInTheDocument();
    // 不提供「上传 PDF / 导入官方链接 / 继续研究」的补资料面板。
    expect(screen.queryByText('研究资料不足')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '继续研究' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '上传并保存' })).not.toBeInTheDocument();
  });

  it('「自动识别来源」解析成功 → 自动填入来源机构并显示提示', async () => {
    mocks.listSourceProviders.mockResolvedValue({ items: [sseProvider], total: 1 });
    mocks.resolveProvider.mockResolvedValue({
      provider_key: 'sse',
      display_name: '上海证券交易所',
      authority_tier: 1,
      critical_claim_eligible: true,
      matched_by: 'issuer_domain',
    });
    renderWithProviders(
      <OrchestrationBanner
        orchestration={withPhase({
          current_phase: 'waiting_manual',
          manual_reason: 'source_acquisition_required',
        })}
        companyId="c1"
      />,
    );
    await screen.findByText('研究资料不足');

    fireEvent.click(screen.getByRole('tab', { name: '导入官方链接' }));
    await userEvent.type(
      screen.getByLabelText('官方 PDF 链接（受控域名）'),
      'https://static.sse.com.cn/annual.pdf',
    );
    await userEvent.click(screen.getByRole('button', { name: '自动识别来源' }));

    await waitFor(() =>
      expect(mocks.resolveProvider).toHaveBeenCalledWith(
        'c1',
        'https://static.sse.com.cn/annual.pdf',
      ),
    );
    expect(await screen.findByText('已识别来源：上海证券交易所（自动匹配）')).toBeInTheDocument();
    // 来源机构 select 已自动填入 sse：提交时 importUrlSource 收到 provider_key='sse'。
    await userEvent.type(screen.getByLabelText('标题'), '贵州茅台 2025 年报');
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
  });

  it('「自动识别来源」解析失败 → 提示手动选择来源机构', async () => {
    mocks.listSourceProviders.mockResolvedValue({ items: [sseProvider], total: 1 });
    mocks.resolveProvider.mockRejectedValue(
      new ApiError(400, 'source_url_not_allowed', 'URL 不在受控域名内', 'req-1'),
    );
    renderWithProviders(
      <OrchestrationBanner
        orchestration={withPhase({
          current_phase: 'waiting_manual',
          manual_reason: 'source_acquisition_required',
        })}
        companyId="c1"
      />,
    );
    await screen.findByText('研究资料不足');

    fireEvent.click(screen.getByRole('tab', { name: '导入官方链接' }));
    await userEvent.type(
      screen.getByLabelText('官方 PDF 链接（受控域名）'),
      'https://example.com/annual.pdf',
    );
    await userEvent.click(screen.getByRole('button', { name: '自动识别来源' }));

    expect(
      await screen.findByText('未能自动识别来源，请手动选择来源机构'),
    ).toBeInTheDocument();
  });

  it('上传 PDF 返回 413 → 显示「文件过大」友好提示', async () => {
    mocks.listSourceProviders.mockResolvedValue({ items: [sseProvider], total: 1 });
    mocks.uploadSourceFile.mockRejectedValue(
      new ApiError(413, 'file_too_large', '请求体过大', 'req-1'),
    );
    renderWithProviders(
      <OrchestrationBanner
        orchestration={withPhase({
          current_phase: 'waiting_manual',
          manual_reason: 'source_acquisition_required',
        })}
        companyId="c1"
      />,
    );
    await screen.findByText('研究资料不足');

    // 选择 PDF 文件（antd Upload 隐藏 input）。
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(['pdf-bytes'], 'annual.pdf', { type: 'application/pdf' });
    fireEvent.change(fileInput, { target: { files: [file] } });

    await userEvent.click(screen.getByRole('combobox', { name: '来源机构' }));
    await screen.findByText('上海证券交易所（sse）');
    await userEvent.click(screen.getByText('上海证券交易所（sse）'));
    await userEvent.type(screen.getByLabelText('标题'), '贵州茅台 2025 年报');

    // 选择文件后提交按钮才可用。
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '上传并保存' })).toBeEnabled(),
    );
    await userEvent.click(screen.getByRole('button', { name: '上传并保存' }));

    expect(await screen.findByText('文件过大：单个 PDF 不能超过 100MB')).toBeInTheDocument();
  });
  it('backflow 闭环：可选面板默认折叠；再次补充研究留空仍发 extra_research（纯自动默认路径）', async () => {
    mocks.getBackflowReview.mockResolvedValue({
      orchestration_id: 'orch-1',
      backflow_human_request_id: 'req-1',
      reason: 'research_backflow_limit_reached',
      decision: null,
      comment: null,
      decided_at: null,
      acceptance_barriers: [],
    });
    mocks.getTaskWorkspace.mockResolvedValue({
      resolved_company: { company_id: 'c1' },
    });
    mocks.actOnBackflowReview.mockResolvedValue({ orchestration_id: 'orch-1' });
    renderWithProviders(
      <OrchestrationBanner
        orchestration={withPhase({
          current_phase: 'research_backflow',
          manual_reason: 'research_backflow_limit_reached',
        })}
        companyId="c1"
      />,
    );
    expect(await screen.findByText('自动补充研究已达到上限')).toBeInTheDocument();
    // 可选面板默认折叠标题存在
    expect(screen.getByText('补充资料（可选 / 附加证据供交叉验证）')).toBeInTheDocument();
    expect(screen.getByText('补充财务数据（可选）')).toBeInTheDocument();
    // 留空直接「再次补充研究」→ 仍触发 extra_research（纯自动）。
    await userEvent.click(screen.getByRole('button', { name: '再次补充研究' }));
    await waitFor(() =>
      expect(mocks.actOnBackflowReview).toHaveBeenCalledWith('orch-1', 'extra_research'),
    );
  });
});
