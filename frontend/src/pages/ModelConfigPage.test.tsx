import { describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';

import { renderWithProviders } from '../test/render';
import type { LlmProviderConfigItem } from '../api/llmProviderConfig';
import { ModelConfigPage } from './ModelConfigPage';

const mocks = vi.hoisted(() => ({
  listLlmConfigs: vi.fn(),
  createLlmConfig: vi.fn(),
  updateLlmConfig: vi.fn(),
  deleteLlmConfig: vi.fn(),
  setActiveLlmConfig: vi.fn(),
  testLlmConnection: vi.fn(),
}));

vi.mock('../api/llmProviderConfig', () => ({
  llmConfigKeys: {
    all: ['llm-configs'],
    list: () => ['llm-configs', 'list'],
  },
  listLlmConfigs: mocks.listLlmConfigs,
  createLlmConfig: mocks.createLlmConfig,
  updateLlmConfig: mocks.updateLlmConfig,
  deleteLlmConfig: mocks.deleteLlmConfig,
  setActiveLlmConfig: mocks.setActiveLlmConfig,
  testLlmConnection: mocks.testLlmConnection,
}));

const config = (overrides: Partial<LlmProviderConfigItem> = {}): LlmProviderConfigItem => ({
  id: 'cfg-1',
  provider: 'deepseek',
  display_name: 'DeepSeek 主模型',
  model_id: 'deepseek-v4-flash',
  base_url: 'https://api.deepseek.com/v1',
  has_api_key: true,
  is_active: true,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  ...overrides,
});

function mockListConfigs(items: LlmProviderConfigItem[]) {
  mocks.listLlmConfigs.mockResolvedValue({
    items,
    total: items.length,
    active_id: items.find((i) => i.is_active)?.id ?? null,
  });
}

beforeEach(() => {
  Object.values(mocks).forEach((m) => m.mockReset());
  mockListConfigs([config()]);
  mocks.deleteLlmConfig.mockResolvedValue(undefined);
  mocks.testLlmConnection.mockResolvedValue({
    ok: true,
    latency_ms: 10,
    message: '连接成功',
  });
});

describe('ModelConfigPage（v1.2.7-B：模型配置中心）', () => {
  it('渲染已配置模型表格、状态与添加按钮', async () => {
    renderWithProviders(<ModelConfigPage />);

    expect(await screen.findByText('模型配置中心')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText('deepseek-v4-flash')).toBeInTheDocument());
    expect(screen.getByText('使用中')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '添加模型配置' })).toBeInTheDocument();
  });

  it('active 配置不显示删除，非 active 配置显示删除并弹出确认', async () => {
    mockListConfigs([config({ id: 'cfg-1', is_active: false })]);
    renderWithProviders(<ModelConfigPage />);
    await waitFor(() => expect(screen.getByText('deepseek-v4-flash')).toBeInTheDocument());

    const deleteButtons = screen.getAllByRole('button', { name: '删除' });
    expect(deleteButtons.length).toBe(1);

    fireEvent.click(deleteButtons[0]);

    expect(await screen.findByText('删除模型配置')).toBeInTheDocument();
    expect(screen.getByText('确认删除该模型配置？删除后无法恢复。')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '确认删除' })).toBeInTheDocument();
  });

  it('点击测试连接调用 testLlmConnection', async () => {
    renderWithProviders(<ModelConfigPage />);
    await waitFor(() => expect(screen.getByText('deepseek-v4-flash')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: '测试连接' }));

    await waitFor(() => expect(mocks.testLlmConnection).toHaveBeenCalledWith('cfg-1'));
  });
});
