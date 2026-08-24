/** 用户模型配置中心 API（v1.2.7-B：LLM provider configs 管理）。
 * 后端 /app/api/v1/routes/llm_provider_configs.py。
 */

import { apiRequest } from './client';

export type LlmProviderType = 'deepseek' | 'openai' | 'openrouter' | 'custom';

export interface LlmProviderConfigItem {
  id: string;
  provider: LlmProviderType;
  display_name: string;
  model_id: string;
  base_url: string | null;
  /** 是否已保存 API Key（永不返回明文）。 */
  has_api_key: boolean;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface LlmConfigListPayload {
  items: LlmProviderConfigItem[];
  total: number;
  active_id: string | null;
}

export interface LlmConfigUpsertPayload {
  provider: LlmProviderType;
  display_name: string;
  model_id: string;
  base_url?: string | null;
  api_key?: string | null;
  is_active?: boolean;
}

export interface LlmConfigTestResult {
  ok: boolean;
  latency_ms?: number | null;
  message: string;
}

export const llmConfigKeys = {
  all: ['llm-configs'] as const,
  list: () => [...llmConfigKeys.all, 'list'] as const,
  detail: (id: string) => [...llmConfigKeys.all, 'detail', id] as const,
};

export async function listLlmConfigs(): Promise<LlmConfigListPayload> {
  return apiRequest<LlmConfigListPayload>('/llm-configs');
}

export async function createLlmConfig(
  payload: LlmConfigUpsertPayload,
): Promise<LlmProviderConfigItem> {
  return apiRequest<LlmProviderConfigItem>('/llm-configs', {
    method: 'POST',
    body: payload,
  });
}

export async function updateLlmConfig(
  configId: string,
  payload: Partial<LlmConfigUpsertPayload>,
): Promise<LlmProviderConfigItem> {
  return apiRequest<LlmProviderConfigItem>(`/llm-configs/${configId}`, {
    method: 'PUT',
    body: payload,
  });
}

export async function deleteLlmConfig(configId: string): Promise<void> {
  await apiRequest<void>(`/llm-configs/${configId}`, { method: 'DELETE' });
}

export async function setActiveLlmConfig(
  configId: string,
  active: boolean,
): Promise<LlmProviderConfigItem> {
  return apiRequest<LlmProviderConfigItem>(`/llm-configs/${configId}/active`, {
    method: 'POST',
    body: { is_active: active },
  });
}

export async function testLlmConnection(
  configId: string,
): Promise<LlmConfigTestResult> {
  return apiRequest<LlmConfigTestResult>(`/llm-configs/${configId}/test`, {
    method: 'POST',
  });
}

export async function testLlmConnectionDraft(
  payload: Partial<LlmConfigUpsertPayload>,
): Promise<LlmConfigTestResult> {
  return apiRequest<LlmConfigTestResult>('/llm-configs/test', {
    method: 'POST',
    body: payload,
  });
}
