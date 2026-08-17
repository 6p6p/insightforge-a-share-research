import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import { createTestQueryClient } from '../test/render';
import type {
  AnalysisArtifactResponse,
  ReportArtifactResponse,
  ReviewsArtifactResponse,
} from '../types/artifacts';
import type { ClaimCitationResponse, EvidenceCitationResponse } from '../types/citation';
import { ApiError } from '../types/api';
import type { TaskWorkspaceResponse } from '../types/workspace';
import { TaskWorkspacePage } from './TaskWorkspacePage';

const mocks = vi.hoisted(() => ({
  getTaskWorkspace: vi.fn(),
  getTaskSources: vi.fn(),
  getTaskEvidence: vi.fn(),
  getTaskAnalysis: vi.fn(),
  getTaskReport: vi.fn(),
  getTaskReviews: vi.fn(),
  getEvidenceCitation: vi.fn(),
  getClaimCitation: vi.fn(),
  getCurrentOrchestration: vi.fn(),
  createOrchestration: vi.fn(),
  useTaskEvents: vi.fn(),
}));

vi.mock('../api/tasks', () => ({
  taskKeys: {
    all: ['tasks'],
    list: () => ['tasks', 'list'],
    detail: (id: string) => ['tasks', 'detail', id],
    workspace: (id: string) => ['tasks', 'workspace', id],
    sources: (id: string, p: unknown) => ['tasks', 'artifacts', id, 'sources', p],
    evidence: (id: string, p: unknown) => ['tasks', 'artifacts', id, 'evidence', p],
    analysis: (id: string) => ['tasks', 'artifacts', id, 'analysis'],
    report: (id: string) => ['tasks', 'artifacts', id, 'report'],
    reviews: (id: string) => ['tasks', 'artifacts', id, 'reviews'],
    citationEvidence: (id: string, cid: string) => ['tasks', 'citations', id, 'evidence', cid],
    citationClaim: (id: string, cid: string) => ['tasks', 'citations', id, 'claims', cid],
  },
  getTaskWorkspace: mocks.getTaskWorkspace,
  getTaskSources: mocks.getTaskSources,
  getTaskEvidence: mocks.getTaskEvidence,
  getTaskAnalysis: mocks.getTaskAnalysis,
  getTaskReport: mocks.getTaskReport,
  getTaskReviews: mocks.getTaskReviews,
  getEvidenceCitation: mocks.getEvidenceCitation,
  getClaimCitation: mocks.getClaimCitation,
}));

vi.mock('../hooks/useTaskEvents', () => ({
  useTaskEvents: mocks.useTaskEvents,
}));

vi.mock('../api/orchestrations', () => ({
  orchestrationKeys: {
    all: ['orchestrations'],
    current: (id: string) => ['orchestrations', 'current', id],
    detail: (id: string) => ['orchestrations', 'detail', id],
  },
  getCurrentOrchestration: mocks.getCurrentOrchestration,
  createOrchestration: mocks.createOrchestration,
}));

vi.mock('../features/workflow-progress/WorkflowProgressPanel', () => ({
  WorkflowProgressPanel: () => <div data-testid="workflow-progress-panel" />,
}));

const workspaceData: TaskWorkspaceResponse = {
  task: {
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
    public_status: 'not_started',
    created_at: '2026-08-11T00:00:00Z',
    updated_at: '2026-08-11T00:00:00Z',
  },
  resolved_company: null,
  current_run: null,
  artifact_summary: {
    source_count: 0,
    evidence_count: 0,
    claim_count: 0,
    report_count: 0,
    review_issue_count: 0,
  },
  research_chain_active: false,
};

function renderPage(initialEntries: string[] = ['/tasks/task-1']): ReturnType<typeof render> {
  return render(
    <ConfigProvider locale={zhCN}>
      <QueryClientProvider client={createTestQueryClient()}>
        <MemoryRouter initialEntries={initialEntries}>
          <Routes>
            <Route path="/tasks/:taskId" element={<TaskWorkspacePage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </ConfigProvider>,
  );
}

beforeEach(() => {
  mocks.getTaskWorkspace.mockReset();
  mocks.getTaskSources.mockReset();
  mocks.getTaskEvidence.mockReset();
  mocks.getTaskAnalysis.mockReset();
  mocks.getTaskReport.mockReset();
  mocks.getTaskReviews.mockReset();
  mocks.getEvidenceCitation.mockReset();
  mocks.getClaimCitation.mockReset();
  mocks.getCurrentOrchestration.mockReset();
  mocks.createOrchestration.mockReset();
  mocks.useTaskEvents.mockReset();
  mocks.useTaskEvents.mockReturnValue({
    events: [],
    connected: true,
    streamEnded: false,
    error: null,
  });
  mocks.getTaskWorkspace.mockResolvedValue(workspaceData);
  // 默认无编排（404 语义 → 组件吞掉返回 null，不渲染 banner）。
  mocks.getCurrentOrchestration.mockResolvedValue(null);
});

describe('TaskWorkspacePage artifact tabs（Stage 6B.1）', () => {
  it('默认显示概览 tab；5 个 artifact getter 初始不触发（惰性挂载）', async () => {
    renderPage();
    expect(await screen.findByText('任务概要')).toBeInTheDocument();
    // 概览 tab 已挂载，artifact tab 未激活 → 不触发任何 artifact query。
    expect(mocks.getTaskSources).not.toHaveBeenCalled();
    expect(mocks.getTaskEvidence).not.toHaveBeenCalled();
    expect(mocks.getTaskAnalysis).not.toHaveBeenCalled();
    expect(mocks.getTaskReport).not.toHaveBeenCalled();
    expect(mocks.getTaskReviews).not.toHaveBeenCalled();
  });

  it('无编排且无进行中的研究 → 「重新启动研究」调用 createOrchestration 并刷新', async () => {
    mocks.createOrchestration.mockResolvedValue({ orchestration_id: 'orch-1' });
    renderPage();
    await screen.findByText('任务概要');

    const restartButton = screen.getByRole('button', { name: '重新启动研究' });
    expect(restartButton).toBeInTheDocument();
    fireEvent.click(restartButton);

    await waitFor(() => expect(mocks.createOrchestration).toHaveBeenCalledWith('task-1'));
    // 成功后刷新 workspace。
    await waitFor(() => expect(mocks.getTaskWorkspace).toHaveBeenCalledWith('task-1'));
  });

  it('切到「来源」tab → 触发 getTaskSources 并渲染来源表格', async () => {
    mocks.getTaskSources.mockResolvedValue({
      items: [
        {
          source_id: 'src-1',
          company_id: 'c1',
          provider_key: 'xinhuanet',
          document_type: 'news_article',
          title: '贵州茅台2026年新闻',
          published_at: '2026-08-01T00:00:00Z',
          reporting_period_end: null,
          source_url: 'https://example.com/news',
          status: 'available',
          created_at: '2026-08-01T00:00:00Z',
          source_identity: 'xinhuanet:https://example.com/news',
          origin_type: 'document_chunk',
          source_type: null,
          label: null,
          fetched_at: null,
          authority_tier: 3,
          locator_summary: 'https://example.com/news',
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    });
    renderPage();
    await screen.findByText('任务概要');

    fireEvent.click(screen.getByRole('tab', { name: '来源' }));
    await waitFor(() => expect(mocks.getTaskSources).toHaveBeenCalledWith('task-1', { limit: 20, offset: 0 }));
    expect(await screen.findByText('贵州茅台2026年新闻')).toBeInTheDocument();
  });

  it('切到「分析」tab → 触发 getTaskAnalysis 并渲染 claims', async () => {
    const analysis: AnalysisArtifactResponse = {
      company_id: 'c1',
      research_question: '2026年营收是否合理？',
      analysis_as_of: '2026-08-10',
      work_items: [
        {
          item_id: 'wi-1',
          analysis_type: 'business',
          evidence_card_ids: ['ev-1'],
          additional_evidence_ids: [],
          macro_driver_evidence_ids: [],
          company_evidence_ids: [],
          calculation_ids: [],
          comparison_ids: [],
          claim_ids: ['cl-1'],
        },
      ],
      claims: [
        {
          claim_id: 'cl-1',
          company_id: 'c1',
          analysis_domain: 'business',
          claim_kind: 'fact',
          statement: '营收增长与行业均值相符',
          confidence: 'high',
          importance: 'high',
          evidence_card_ids: ['ev-1'],
          analyst_name: null,
        },
      ],
      synthesis_id: 'sy-1',
      synthesis_result_id: 'sy-result-1',
      synthesis_fingerprint: 'fp-1',
      result_fingerprint: 'rf-1',
      themes: [
        { title: '增长动力', summary: '营收增长与行业均值相符。', claim_ids: ['cl-1'] },
      ],
      conflicts: [],
      evidence_gaps: [],
      work_items_available: true,
    };
    mocks.getTaskAnalysis.mockResolvedValue(analysis);
    renderPage();
    await screen.findByText('任务概要');

    fireEvent.click(screen.getByRole('tab', { name: '分析' }));
    await waitFor(() => expect(mocks.getTaskAnalysis).toHaveBeenCalledWith('task-1'));
    expect(await screen.findByText('营收增长与行业均值相符')).toBeInTheDocument();
  });

  it('分析 tab 遇到完整性错误 → 显示「产物完整性校验失败」', async () => {
    mocks.getTaskAnalysis.mockRejectedValue(
      new ApiError(409, 'task_artifact_integrity', '任务产物完整性校验失败', 'req-1'),
    );
    renderPage();
    await screen.findByText('任务概要');

    fireEvent.click(screen.getByRole('tab', { name: '分析' }));
    expect(await screen.findByText('产物完整性校验失败')).toBeInTheDocument();
  });
});

// ------------------------------------------------------------------ 6B.2 fixtures

const reportData: ReportArtifactResponse = {
  report_id: 'rpt-1',
  outline_id: 'out-1',
  company_id: 'c1',
  research_question_sha256: 'rq-1',
  analysis_as_of: '2026-08-10',
  report_schema_version: 1,
  report_fingerprint: 'fp-rpt-1',
  section_count: 1,
  sections: [
    {
      section_id: 'S2',
      draft_section_id: 'ds-1',
      section_order: 1,
      section_type: 'analysis',
      title: '营收分析',
      paragraphs: [
        {
          paragraph_index: 3,
          text: '2026 年营收增长与行业均值相符。',
          claim_ids: ['cl-claim-a'],
          evidence_card_ids: ['ev-card-a'],
          conflict_indexes: [],
          evidence_gap_indexes: [],
        },
      ],
    },
  ],
};

const claimCitationData: ClaimCitationResponse = {
  claim_id: 'cl-claim-a',
  statement: '2026 年营收增长与行业均值相符。',
  domain: 'business',
  kind: 'fact',
  confidence: 'high',
  importance: 'high',
  evidence_relations: [
    { evidence_card_id: 'ev-card-a', evidence_statement: '行业均值数据', relation: 'supports' },
  ],
};

const evidenceCitationData: EvidenceCitationResponse = {
  evidence: {
    evidence_card_id: 'ev-card-a',
    statement: '行业均值数据',
    quote_text: '营收增长 12%',
    evidence_type: 'financial',
    origin_type: 'document_chunk',
  },
  claim_relations: [
    { claim_id: 'cl-claim-a', claim_statement: '2026 年营收增长与行业均值相符。', relation: 'supports' },
  ],
  provenance: {
    origin_type: 'document_chunk',
    source_id: 'src-1',
    provider_key: 'xinhuanet',
    provider_label: '新华网',
    title: '贵州茅台 2026 半年报',
    source_url: 'https://example.com/report',
    published_at: '2026-08-01T00:00:00Z',
    authority_tier: 3,
    document_type: 'annual_report',
    raw_artifact_id: 'raw-1',
    media_type: 'application/pdf',
    parsed_source_id: 'ps-1',
    chunk_id: 'chunk-1',
    locator: {
      locator_type: 'pdf_page',
      block_ordinal: null,
      char_start: null,
      char_end: null,
      ordinal: null,
      tag: null,
      xpath: null,
      element_id: null,
      page_number: 2,
      line_index: 5,
      bbox: null,
      page_width: null,
      page_height: null,
    },
    locator_refs: [],
    context_text: '…营收增长 12%…',
    quote_text: '营收增长 12%',
  },
};

describe('TaskWorkspacePage citation navigation（Stage 6B.2 spec O/P/Q）', () => {
  it('报告 tab 点击观点 Tag → 打开 claim citation 抽屉', async () => {
    mocks.getTaskReport.mockResolvedValue(reportData);
    mocks.getClaimCitation.mockResolvedValue(claimCitationData);
    renderPage();
    await screen.findByText('任务概要');

    fireEvent.click(screen.getByRole('tab', { name: '报告' }));
    await screen.findByText('营收分析');
    fireEvent.click(screen.getByText('观点 cl-claim'));

    expect(await screen.findByText('支撑该观点的证据（1）')).toBeInTheDocument();
    expect(mocks.getClaimCitation).toHaveBeenCalledWith('task-1', 'cl-claim-a');
  });

  it('报告 tab 点击证据 Tag → 打开 evidence citation 抽屉并渲染 Document provenance', async () => {
    mocks.getTaskReport.mockResolvedValue(reportData);
    mocks.getEvidenceCitation.mockResolvedValue(evidenceCitationData);
    renderPage();
    await screen.findByText('任务概要');

    fireEvent.click(screen.getByRole('tab', { name: '报告' }));
    await screen.findByText('营收分析');
    fireEvent.click(screen.getByText('证据 ev-card-'));

    expect(await screen.findByText('来源追溯（Document）')).toBeInTheDocument();
    expect(mocks.getEvidenceCitation).toHaveBeenCalledWith('task-1', 'ev-card-a');
  });

  it('证据 tab「查看引用」→ 打开 evidence citation 抽屉', async () => {
    mocks.getTaskEvidence.mockResolvedValue({
      items: [
        {
          evidence_card_id: 'ev-card-a',
          source_id: 'src-1',
          company_id: 'c1',
          evidence_statement: '行业均值数据',
          evidence_type: 'financial',
          extractor_confidence: 'high',
          quote_text: null,
          origin_type: 'document_chunk',
          created_at: '2026-08-01T00:00:00Z',
          used_by_claim_ids: ['cl-claim-a'],
          claim_relations: [{ claim_id: 'cl-claim-a', relation: 'supports' }],
          macro_observation_id: null,
          macro_snapshot_id: null,
          macro_series_id: null,
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    });
    mocks.getEvidenceCitation.mockResolvedValue(evidenceCitationData);
    renderPage();
    await screen.findByText('任务概要');

    fireEvent.click(screen.getByRole('tab', { name: '证据' }));
    await screen.findByText('行业均值数据');
    fireEvent.click(screen.getByRole('button', { name: '查看引用' }));

    expect(mocks.getEvidenceCitation).toHaveBeenCalledWith('task-1', 'ev-card-a');
    expect(await screen.findByText('来源追溯（Document）')).toBeInTheDocument();
  });

  it('审核 tab「定位报告」→ 切到报告 tab 并高亮定位段落', async () => {
    const reviewsData: ReviewsArtifactResponse = {
      audit_id: 'audit-1',
      report_id: 'rpt-1',
      audit_status: 'completed',
      recommended_route: 'approve',
      issue_count: 1,
      audit_fingerprint: 'fp-audit-1',
      issues: [
        {
          review_issue_id: 'issue-1',
          ordinal: 1,
          issue_type: 'missing_evidence',
          severity: 'major',
          section_id: 'S2',
          paragraph_index: 3,
          message: '缺少关键证据',
          related_claim_ids: [],
          related_evidence_card_ids: [],
        },
      ],
      check: null,
      review_action: null,
      human_review: null,
      research_backflow: null,
    };
    mocks.getTaskReviews.mockResolvedValue(reviewsData);
    mocks.getTaskReport.mockResolvedValue(reportData);
    renderPage();
    await screen.findByText('任务概要');

    fireEvent.click(screen.getByRole('tab', { name: '审核' }));
    await screen.findByText('缺少关键证据');
    fireEvent.click(screen.getByRole('button', { name: '定位报告' }));

    await screen.findByText('营收分析');
    const located = document.getElementById('report-para-S2:3');
    expect(located).not.toBeNull();
    // jsdom 会把 #fffbe6 序列化为 rgb(255, 251, 230)
    expect(located?.getAttribute('style')).toContain('rgb(255, 251, 230)');
  });

  it('section-only 定位（?tab=report&section=S2，无 paragraph）→ 高亮整节容器（Gate C）', async () => {
    mocks.getTaskReport.mockResolvedValue(reportData);
    renderPage(['/tasks/task-1?tab=report&section=S2']);

    await screen.findByText('营收分析');
    const sectionEl = document.getElementById('report-section-S2');
    expect(sectionEl).not.toBeNull();
    expect(sectionEl?.getAttribute('style')).toContain('rgb(255, 251, 230)');
    // 不伪造 paragraph -1：不存在 report-para-S2:-1
    expect(document.getElementById('report-para-S2:-1')).toBeNull();
  });

  it('locator 目标不存在 → 显示轻量警告，不静默不 crash（Gate D）', async () => {
    mocks.getTaskReport.mockResolvedValue(reportData);
    renderPage(['/tasks/task-1?tab=report&section=S999&paragraph=10']);

    await screen.findByText('营收分析');
    expect(
      await screen.findByText('未找到对应报告位置，报告版本可能已变化。'),
    ).toBeInTheDocument();
    // 没有自动高亮任何真实段落
    expect(document.getElementById('report-para-S999:10')).toBeNull();
    const s2 = document.getElementById('report-section-S2');
    expect(s2?.getAttribute('style') ?? '').not.toContain('rgb(255, 251, 230)');
  });
});
