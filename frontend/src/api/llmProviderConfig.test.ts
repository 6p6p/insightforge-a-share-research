import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./client')>();
  return { ...actual, apiRequest: vi.fn() };
});

import { apiRequest } from './client';
import {
  createLlmConfig,
  deleteLlmConfig,
  listLlmConfigs,
  llmConfigKeys,
  setActiveLlmConfig,
  testLlmConnection,
  testLlmConnectionDraft,
  updateLlmConfig,
} from './llmProviderConfig';

const mockedApiRequest = vi.mocked(apiRequest);

describe('llmProviderConfig API（v1.2.7-B：模型配置中心）', () => {
  beforeEach(() => {
    mockedApiRequest.mockReset();
  });

  it('llmConfigKeys 覆盖 list/detail key', () => {
    expect(llmConfigKeys.list()).toEqual(['llm-configs', 'list']);
    expect(llmConfigKeys.detail('c1')).toEqual(['llm-configs', 'detail', 'c1']);
  });

  it('listLlmConfigs → GET /llm-configs', async () => {
    mockedApiRequest.mockResolvedValue({ items: [], total: 0, active_id: null });
    await listLlmConfigs();
    expect(mockedApiRequest).toHaveBeenCalledWith('/llm-configs');
  });

  it('createLlmConfig → POST /llm-configs + payload', async () => {
    mockedApiRequest.mockResolvedValue({ id: 'c1' });
    await createLlmConfig({ provider: 'openai', display_name: 'OpenAI', model_id: 'gpt-4o-mini' });
    expect(mockedApiRequest).toHaveBeenCalledWith('/llm-configs', {
      method: 'POST',
      body: { provider: 'openai', display_name: 'OpenAI', model_id: 'gpt-4o-mini' },
    });
  });

  it('updateLlmConfig → PUT /llm-configs/{id}', async () => {
    mockedApiRequest.mockResolvedValue({ id: 'c1' });
    await updateLlmConfig('c1', { display_name: '新版' });
    expect(mockedApiRequest).toHaveBeenCalledWith('/llm-configs/c1', {
      method: 'PUT',
      body: { display_name: '新版' },
    });
  });

  it('deleteLlmConfig → DELETE /llm-configs/{id}', async () => {
    mockedApiRequest.mockResolvedValue(undefined);
    await deleteLlmConfig('c1');
    expect(mockedApiRequest).toHaveBeenCalledWith('/llm-configs/c1', { method: 'DELETE' });
  });

  it('setActiveLlmConfig → POST /llm-configs/{id}/active', async () => {
    mockedApiRequest.mockResolvedValue({ id: 'c1', is_active: true });
    await setActiveLlmConfig('c1', true);
    expect(mockedApiRequest).toHaveBeenCalledWith('/llm-configs/c1/active', {
      method: 'POST',
      body: { is_active: true },
    });
  });

  it('testLlmConnection → POST /llm-configs/{id}/test', async () => {
    mockedApiRequest.mockResolvedValue({ ok: true, latency_ms: 9, message: '连接成功' });
    await testLlmConnection('c1');
    expect(mockedApiRequest).toHaveBeenCalledWith('/llm-configs/c1/test', { method: 'POST' });
  });

  it('testLlmConnectionDraft → POST /llm-configs/test + payload', async () => {
    mockedApiRequest.mockResolvedValue({ ok: true, latency_ms: 5, message: '连接成功' });
    await testLlmConnectionDraft({ provider: 'custom', model_id: 'm', api_key: 'k' });
    expect(mockedApiRequest).toHaveBeenCalledWith('/llm-configs/test', {
      method: 'POST',
      body: { provider: 'custom', model_id: 'm', api_key: 'k' },
    });
  });
});