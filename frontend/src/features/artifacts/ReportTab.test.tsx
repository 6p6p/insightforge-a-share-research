/** ReportTab「导出报告」Dropdown（Stage 6C spec Q）。

mock `api/tasks`：报告加载走 `getTaskReport`；导出走 `createExport` +
`downloadExportContent`。断言：

1. 报告存在 → 渲染「导出报告」按钮；
2. 点击展开 Dropdown → 3 项（Markdown / Word / PDF）；
3. 选 Markdown → 调用 `createExport(taskId, 'markdown')` + 触发浏览器下载
   （`URL.createObjectURL` → `<a download>` click）；
4. 409 不可导出 → 内联警告（不假装成功、不触发下载）。
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { MemoryRouter } from 'react-router-dom';

import { createTestQueryClient } from '../../test/render';
import { ApiError } from '../../types/api';
import type { ReportArtifactResponse } from '../../types/artifacts';
import { ReportTab } from './ReportTab';

const mocks = vi.hoisted(() => ({
  getTaskReport: vi.fn(),
  createExport: vi.fn(),
  downloadExportContent: vi.fn(),
}));

vi.mock('../../api/tasks', () => ({
  taskKeys: {
    all: ['tasks'],
    report: (id: string) => ['tasks', 'artifacts', id, 'report'],
  },
  getTaskReport: mocks.getTaskReport,
  createExport: mocks.createExport,
  downloadExportContent: mocks.downloadExportContent,
}));

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

function renderReportTab(): ReturnType<typeof render> {
  return render(
    <ConfigProvider locale={zhCN}>
      <QueryClientProvider client={createTestQueryClient()}>
        <MemoryRouter>
          <ReportTab taskId="task-1" />
        </MemoryRouter>
      </QueryClientProvider>
    </ConfigProvider>,
  );
}

describe('ReportTab export dropdown（Stage 6C spec Q）', () => {
  beforeEach(() => {
    mocks.getTaskReport.mockReset();
    mocks.createExport.mockReset();
    mocks.downloadExportContent.mockReset();
  });

  it('报告存在 → 渲染「导出报告」按钮；展开显示 3 种格式', async () => {
    mocks.getTaskReport.mockResolvedValue(reportData);
    renderReportTab();

    const button = await screen.findByRole('button', { name: /导出报告/ });
    expect(button).toBeTruthy();

    fireEvent.click(button);
    expect(await screen.findByText('Markdown（.md）')).toBeTruthy();
    expect(screen.getByText('Word（.docx）')).toBeTruthy();
    expect(screen.getByText('PDF（.pdf）')).toBeTruthy();
  });

  it('选 Markdown → createExport + 下载 content 并触发浏览器下载', async () => {
    mocks.getTaskReport.mockResolvedValue(reportData);
    mocks.createExport.mockResolvedValue({
      export_id: 'exp-1',
      format: 'markdown',
      file_name: 'report_rpt-1.md',
      media_type: 'text/markdown',
      byte_size: 12,
      replayed: false,
      created_at: '2026-08-11T00:00:00Z',
    });
    mocks.downloadExportContent.mockResolvedValue({
      blob: new Blob(['# hello'], { type: 'text/markdown' }),
      fileName: 'report_rpt-1.md',
    });

    // stub 浏览器下载：URL.createObjectURL + <a>.click。
    const createObjectURL = vi.fn(() => 'blob:mock-url');
    const revokeObjectURL = vi.fn();
    vi.stubGlobal('URL', { ...URL, createObjectURL, revokeObjectURL });
    const clickSpy = vi.fn();
    const originalCreateElement = document.createElement.bind(document);
    vi.spyOn(document, 'createElement').mockImplementation((tag) => {
      const el = originalCreateElement(tag);
      if (tag === 'a') {
        Object.defineProperty(el, 'click', { value: clickSpy, writable: true });
      }
      return el;
    });

    renderReportTab();
    const button = await screen.findByRole('button', { name: /导出报告/ });
    fireEvent.click(button);
    fireEvent.click(await screen.findByText('Markdown（.md）'));

    await waitFor(() => expect(mocks.createExport).toHaveBeenCalledWith('task-1', 'markdown'));
    expect(mocks.downloadExportContent).toHaveBeenCalledWith('task-1', 'exp-1');
    await waitFor(() => expect(clickSpy).toHaveBeenCalledTimes(1));
    expect(createObjectURL).toHaveBeenCalled();
    expect(revokeObjectURL).toHaveBeenCalled();

    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('409 不可导出 → 内联警告，不触发下载', async () => {
    mocks.getTaskReport.mockResolvedValue(reportData);
    mocks.createExport.mockRejectedValue(
      new ApiError(409, 'report_not_exportable', '报告当前不可导出', 'req-1'),
    );
    mocks.downloadExportContent.mockResolvedValue({
      blob: new Blob(['x']),
      fileName: 'report_rpt-1.md',
    });
    const clickSpy = vi.fn();
    const originalCreateElement = document.createElement.bind(document);
    vi.spyOn(document, 'createElement').mockImplementation((tag) => {
      const el = originalCreateElement(tag);
      if (tag === 'a') {
        Object.defineProperty(el, 'click', { value: clickSpy, writable: true });
      }
      return el;
    });

    renderReportTab();
    const button = await screen.findByRole('button', { name: /导出报告/ });
    fireEvent.click(button);
    fireEvent.click(await screen.findByText('Markdown（.md）'));

    expect(await screen.findByText('报告当前不可导出（审核未通过 / 校验未通过 / 仍在运行）。')).toBeTruthy();
    expect(mocks.downloadExportContent).not.toHaveBeenCalled();
    expect(clickSpy).not.toHaveBeenCalled();

    vi.restoreAllMocks();
  });
});
