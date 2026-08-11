import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import { createTestQueryClient } from '../test/render';
import type { AnalysisArtifactResponse } from '../types/artifacts';
import type { TaskWorkspaceResponse } from '../types/workspace';
import { TaskWorkspacePage } from './TaskWorkspacePage';

const mocks = vi.hoisted(() => ({
  getTaskWorkspace: vi.fn(),
  getTaskSources: vi.fn(),
  getTaskEvidence: vi.fn(),
  getTaskAnalysis: vi.fn(),
  getTaskReport: vi.fn(),
  getTaskReviews: vi.fn(),
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
  },
  getTaskWorkspace: mocks.getTaskWorkspace,
  getTaskSources: mocks.getTaskSources,
  getTaskEvidence: mocks.getTaskEvidence,
  getTaskAnalysis: mocks.getTaskAnalysis,
  getTaskReport: mocks.getTaskReport,
  getTaskReviews: mocks.getTaskReviews,
}));

vi.mock('../hooks/useTaskEvents', () => ({
  useTaskEvents: mocks.useTaskEvents,
}));

vi.mock('../features/workflow-progress/StartResearchPanel', () => ({
  StartResearchPanel: () => <div data-testid="start-research-panel" />,
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

function renderPage(): ReturnType<typeof render> {
  return render(
    <ConfigProvider locale={zhCN}>
      <QueryClientProvider client={createTestQueryClient()}>
        <MemoryRouter initialEntries={['/tasks/task-1']}>
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
  mocks.useTaskEvents.mockReset();
  mocks.useTaskEvents.mockReturnValue({
    events: [],
    connected: true,
    streamEnded: false,
    error: null,
  });
  mocks.getTaskWorkspace.mockResolvedValue(workspaceData);
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
      synthesis_fingerprint: 'fp-1',
    };
    mocks.getTaskAnalysis.mockResolvedValue(analysis);
    renderPage();
    await screen.findByText('任务概要');

    fireEvent.click(screen.getByRole('tab', { name: '分析' }));
    await waitFor(() => expect(mocks.getTaskAnalysis).toHaveBeenCalledWith('task-1'));
    expect(await screen.findByText('营收增长与行业均值相符')).toBeInTheDocument();
  });
});
