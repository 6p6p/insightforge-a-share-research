import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./client')>();
  return { ...actual, apiRequest: vi.fn() };
});

import { apiRequest } from './client';
import {
  createExport,
  downloadExportContent,
  getClaimCitation,
  getEvidenceCitation,
  getTaskAnalysis,
  getTaskEvidence,
  getTaskReport,
  getTaskReviews,
  getTaskSources,
  taskKeys,
} from './tasks';

const mockedApiRequest = vi.mocked(apiRequest);

describe('task artifact API（Stage 6B.1）', () => {
  beforeEach(() => {
    mockedApiRequest.mockReset();
  });

  it('taskKeys 覆盖 5 个任务级 artifact key', () => {
    expect(taskKeys.sources('t1', { limit: 10, offset: 20 })).toEqual([
      'tasks', 'artifacts', 't1', 'sources', { limit: 10, offset: 20 },
    ]);
    expect(taskKeys.evidence('t1', { limit: 10, offset: 20 })).toEqual([
      'tasks', 'artifacts', 't1', 'evidence', { limit: 10, offset: 20 },
    ]);
    expect(taskKeys.analysis('t1')).toEqual(['tasks', 'artifacts', 't1', 'analysis']);
    expect(taskKeys.report('t1')).toEqual(['tasks', 'artifacts', 't1', 'report']);
    expect(taskKeys.reviews('t1')).toEqual(['tasks', 'artifacts', 't1', 'reviews']);
  });

  it('getTaskSources 组装分页 query', async () => {
    mockedApiRequest.mockResolvedValue({ items: [], total: 0, limit: 10, offset: 20 });
    await getTaskSources('t1', { limit: 10, offset: 20 });
    expect(mockedApiRequest).toHaveBeenCalledWith('/tasks/t1/sources?limit=10&offset=20');
  });

  it('getTaskEvidence 组装分页 query', async () => {
    mockedApiRequest.mockResolvedValue({ items: [], total: 0, limit: 50, offset: 0 });
    await getTaskEvidence('t1', { limit: 50, offset: 0 });
    expect(mockedApiRequest).toHaveBeenCalledWith('/tasks/t1/evidence?limit=50&offset=0');
  });

  it('getTaskSources 缺省 limit/offset 使用默认 20/0', async () => {
    mockedApiRequest.mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0 });
    await getTaskSources('t1');
    expect(mockedApiRequest).toHaveBeenCalledWith('/tasks/t1/sources?limit=20&offset=0');
  });

  it('getTaskAnalysis / getTaskReport / getTaskReviews 无 query 参数', async () => {
    mockedApiRequest.mockResolvedValue({});
    await getTaskAnalysis('t1');
    await getTaskReport('t1');
    await getTaskReviews('t1');
    expect(mockedApiRequest).toHaveBeenCalledWith('/tasks/t1/analysis');
    expect(mockedApiRequest).toHaveBeenCalledWith('/tasks/t1/report');
    expect(mockedApiRequest).toHaveBeenCalledWith('/tasks/t1/reviews');
  });
});

describe('export API（Stage 6C spec P）', () => {
  beforeEach(() => {
    mockedApiRequest.mockReset();
  });

  it('taskKeys.exports → 任务级 export key', () => {
    expect(taskKeys.exports('t1')).toEqual(['tasks', 'exports', 't1']);
  });

  it('createExport → POST /tasks/{task}/export + {format}', async () => {
    mockedApiRequest.mockResolvedValue({
      export_id: 'exp-1',
      format: 'markdown',
      file_name: 'report_x.md',
      media_type: 'text/markdown',
      byte_size: 12,
      replayed: false,
      created_at: '2026-08-11T00:00:00Z',
    });
    await createExport('t1', 'pdf');
    expect(mockedApiRequest).toHaveBeenCalledWith('/tasks/t1/export', {
      method: 'POST',
      body: { format: 'pdf' },
    });
  });

  it('downloadExportContent → content 端点 + 解析 Content-Disposition 文件名', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      blob: () => Promise.resolve(new Blob(['bytes'])),
      headers: {
        get: () => 'attachment; filename="report_x.pdf"',
      },
    }));
    const { blob, fileName } = await downloadExportContent('t1', 'exp-1');
    expect(globalThis.fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/tasks/t1/exports/exp-1/content'),
      expect.objectContaining({ headers: { Accept: 'application/octet-stream' } }),
    );
    expect(fileName).toBe('report_x.pdf');
    expect(blob.size).toBe(5);
    vi.unstubAllGlobals();
  });
});

describe('citation API（Stage 6B.2 spec K/L）', () => {
  beforeEach(() => {
    mockedApiRequest.mockReset();
  });

  it('taskKeys 覆盖 evidence/claim citation key（task-scoped）', () => {
    expect(taskKeys.citationEvidence('t1', 'ev-1')).toEqual([
      'tasks', 'citations', 't1', 'evidence', 'ev-1',
    ]);
    expect(taskKeys.citationClaim('t1', 'cl-1')).toEqual([
      'tasks', 'citations', 't1', 'claims', 'cl-1',
    ]);
  });

  it('getEvidenceCitation → /tasks/{task}/citations/evidence/{card}', async () => {
    mockedApiRequest.mockResolvedValue({ evidence: {}, claim_relations: [], provenance: {} });
    await getEvidenceCitation('t1', 'ev-1');
    expect(mockedApiRequest).toHaveBeenCalledWith('/tasks/t1/citations/evidence/ev-1');
  });

  it('getClaimCitation → /tasks/{task}/citations/claims/{claim}', async () => {
    mockedApiRequest.mockResolvedValue({ claim_id: 'cl-1', evidence_relations: [] });
    await getClaimCitation('t1', 'cl-1');
    expect(mockedApiRequest).toHaveBeenCalledWith('/tasks/t1/citations/claims/cl-1');
  });
});
