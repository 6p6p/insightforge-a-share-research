/** ReviewsTab「审核」视图 3-case（P0/P2 一致性修复）。

C1: 有 report_audit → 渲染审核概览（audit_status / 建议路由 / 问题数）；
C2: 无 audit_id 但有 pending_human_review（audit-degraded / backflow manual
    closure）→ 显示「该报告需要人工确认」+ 可理解原因，绝不误报「无审核记录」；
C3: 无 audit 且无 pending_human_review → 才显示「该任务尚无审核记录」。
*/

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { MemoryRouter } from 'react-router-dom';

import { createTestQueryClient } from '../../test/render';
import type { ReviewsArtifactResponse } from '../../types/artifacts';
import { ReviewsTab } from './ReviewsTab';

const mocks = vi.hoisted(() => ({
  getTaskReviews: vi.fn(),
}));

vi.mock('../../api/tasks', () => ({
  taskKeys: {
    all: ['tasks'],
    reviews: (id: string) => ['tasks', 'artifacts', id, 'reviews'],
    report: (id: string) => ['tasks', 'artifacts', id, 'report'],
  },
  getTaskReviews: mocks.getTaskReviews,
}));

function renderReviewsTab(): ReturnType<typeof render> {
  return render(
    <ConfigProvider locale={zhCN}>
      <QueryClientProvider client={createTestQueryClient()}>
        <MemoryRouter>
          <ReviewsTab taskId="task-1" />
        </MemoryRouter>
      </QueryClientProvider>
    </ConfigProvider>,
  );
}

const base = (): ReviewsArtifactResponse => ({
  audit_id: null,
  report_id: null,
  audit_status: null,
  recommended_route: null,
  issue_count: 0,
  audit_fingerprint: null,
  issues: [],
  check: null,
  review_action: null,
  human_review: null,
  research_backflow: null,
  pending_human_review: null,
});

describe('ReviewsTab 3-case（P0/P2 一致性修复）', () => {
  beforeEach(() => {
    mocks.getTaskReviews.mockReset();
  });

  it('C1 有 report_audit → 渲染审核概览，不显示「无审核记录」', async () => {
    mocks.getTaskReviews.mockResolvedValue({
      ...base(),
      audit_id: 'aud-1',
      report_id: 'rpt-1',
      audit_status: 'fail',
      recommended_route: 'human_review',
      issue_count: 1,
      issues: [
        {
          review_issue_id: 'iss-1',
          ordinal: 1,
          issue_type: 'evidence_mismatch',
          severity: 'critical',
          section_id: 'S2',
          paragraph_index: 3,
          message: '数据与证据不符',
          related_claim_ids: [],
          related_evidence_card_ids: [],
        },
      ],
      check: { check_result_id: 'ck-1', status: 'pass', findings: [] },
    });
    renderReviewsTab();
    expect(await screen.findByText('审核概览')).toBeTruthy();
    expect(screen.getByText('未通过')).toBeTruthy();
    expect(screen.queryByText('该任务尚无审核记录（报告审核尚未完成）。')).toBeNull();
  });

  it('C2 无 audit 但人工复核等待（audit-degraded）→ 需要人工确认 + 原因', async () => {
    mocks.getTaskReviews.mockResolvedValue({
      ...base(),
      pending_human_review: {
        reason: 'report_audit_unavailable',
        decision: null,
        comment: null,
        decided_at: null,
      },
    });
    renderReviewsTab();
    expect(await screen.findByText('该报告需要人工确认')).toBeTruthy();
    expect(
      screen.getByText('自动审核失败（报告审计未完成），需要重新验证'),
    ).toBeTruthy();
    expect(screen.queryByText('该任务尚无审核记录（报告审核尚未完成）。')).toBeNull();
  });

  it('C3 无 audit 且无人工复核等待 → 才显示「尚无审核记录」', async () => {
    mocks.getTaskReviews.mockResolvedValue(base());
    renderReviewsTab();
    expect(
      await screen.findByText('该任务尚无审核记录（报告审核尚未完成）。'),
    ).toBeTruthy();
    expect(screen.queryByText('该报告需要人工确认')).toBeNull();
  });
});
