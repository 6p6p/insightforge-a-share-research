import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./client')>();
  return { ...actual, apiRequest: vi.fn() };
});

import { apiRequest } from './client';
import { importUrlSource, listSourceProviders, sourceKeys, uploadSourceFile } from './sources';

const mockedApiRequest = vi.mocked(apiRequest);

describe('source 受控获取 API（7A Product Gate spec I）', () => {
  beforeEach(() => {
    mockedApiRequest.mockReset();
  });

  it('sourceKeys.providers 支持 enabledOnly 参数', () => {
    expect(sourceKeys.providers()).toEqual(['sources', 'providers', { enabledOnly: true }]);
    expect(sourceKeys.providers({ enabledOnly: false })).toEqual([
      'sources', 'providers', { enabledOnly: false },
    ]);
  });

  it('uploadSourceFile → POST /source-records/upload（multipart FormData）', async () => {
    mockedApiRequest.mockResolvedValue({ source_id: 'src-1' });
    const file = new File(['pdf-bytes'], 'annual.pdf', { type: 'application/pdf' });
    await uploadSourceFile({
      company_id: 'c1',
      provider_key: 'sse',
      document_type: 'annual_report',
      title: '贵州茅台 2025 年报',
      source_url: 'https://static.sse.com.cn/a.pdf',
      file,
    });
    expect(mockedApiRequest).toHaveBeenCalledTimes(1);
    const [path, options] = mockedApiRequest.mock.calls[0];
    expect(path).toBe('/source-records/upload');
    expect(options).toMatchObject({ method: 'POST' });
    const body = (options as { body: FormData }).body;
    expect(body).toBeInstanceOf(FormData);
    expect(body.get('company_id')).toBe('c1');
    expect(body.get('provider_key')).toBe('sse');
    expect(body.get('document_type')).toBe('annual_report');
    expect(body.get('title')).toBe('贵州茅台 2025 年报');
    expect(body.get('source_url')).toBe('https://static.sse.com.cn/a.pdf');
    expect(body.get('file')).toBe(file);
  });

  it('uploadSourceFile 可选字段为空时不 append', async () => {
    mockedApiRequest.mockResolvedValue({ source_id: 'src-1' });
    await uploadSourceFile({
      company_id: 'c1',
      provider_key: 'sse',
      document_type: 'other',
      title: 't',
      source_url: 'u',
      file: new File(['x'], 'x.pdf'),
    });
    const body = (mockedApiRequest.mock.calls[0][1] as { body: FormData }).body;
    expect(body.get('published_at')).toBeNull();
    expect(body.get('external_document_id')).toBeNull();
  });

  it('importUrlSource → POST /source-records/import-url（JSON）', async () => {
    mockedApiRequest.mockResolvedValue({ source_id: 'src-2' });
    await importUrlSource({
      company_id: 'c1',
      provider_key: 'sse',
      document_type: 'annual_report',
      title: '贵州茅台 2025 年报',
      source_url: 'https://static.sse.com.cn/a.pdf',
    });
    expect(mockedApiRequest).toHaveBeenCalledWith('/source-records/import-url', {
      method: 'POST',
      body: {
        company_id: 'c1',
        provider_key: 'sse',
        document_type: 'annual_report',
        title: '贵州茅台 2025 年报',
        source_url: 'https://static.sse.com.cn/a.pdf',
      },
    });
  });

  it('listSourceProviders → GET /source-providers?enabled_only=true', async () => {
    mockedApiRequest.mockResolvedValue({ items: [], total: 0 });
    await listSourceProviders();
    expect(mockedApiRequest).toHaveBeenCalledWith('/source-providers?enabled_only=true');
  });
});
