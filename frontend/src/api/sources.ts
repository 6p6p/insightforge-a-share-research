/** Source 受控获取 API（后端 /app/api/v1/routes/source_records.py + source_registry.py）。

复用既有 PDF upload / 受控 URL import / source-provider 注册表能力
（7A Product Gate spec I）。upload 用 FormData；import-url 用 JSON。
 */

import { apiRequest } from './client';
import type {
  SourceDocumentType,
  SourceProviderListResponse,
  SourceRecordResponse,
  SourceUrlImportRequest,
} from '../types/source';

/** TanStack Query 查询键（provider 注册表只读，60s staleTime 缓存）。 */
export const sourceKeys = {
  all: ['sources'] as const,
  providers: (params: { enabledOnly?: boolean } = { enabledOnly: true }) =>
    [...sourceKeys.all, 'providers', params] as const,
};

/** 上传 PDF（multipart）到 raw artifact store；返回入库的 SourceRecord。 */
export async function uploadSourceFile(payload: {
  company_id: string;
  provider_key: string;
  document_type: SourceDocumentType;
  title: string;
  source_url: string;
  file: File;
  published_at?: string | null;
  reporting_period_end?: string | null;
  external_document_id?: string | null;
}): Promise<SourceRecordResponse> {
  const form = new FormData();
  form.append('company_id', payload.company_id);
  form.append('provider_key', payload.provider_key);
  form.append('document_type', payload.document_type);
  form.append('title', payload.title);
  form.append('source_url', payload.source_url);
  form.append('file', payload.file);
  if (payload.published_at) {
    form.append('published_at', payload.published_at);
  }
  if (payload.reporting_period_end) {
    form.append('reporting_period_end', payload.reporting_period_end);
  }
  if (payload.external_document_id) {
    form.append('external_document_id', payload.external_document_id);
  }
  return apiRequest<SourceRecordResponse>('/source-records/upload', {
    method: 'POST',
    body: form,
  });
}

/** 受控 URL 导入（source-registry-approved domain 校验在服务端）。 */
export async function importUrlSource(
  payload: SourceUrlImportRequest,
): Promise<SourceRecordResponse> {
  return apiRequest<SourceRecordResponse>('/source-records/import-url', {
    method: 'POST',
    body: payload,
  });
}

/** 启用中的 source provider 注册表（enabled_only 默认 true）。 */
export async function listSourceProviders(params: {
  enabledOnly?: boolean;
} = {}): Promise<SourceProviderListResponse> {
  const query = new URLSearchParams();
  query.set('enabled_only', String(params.enabledOnly ?? true));
  return apiRequest<SourceProviderListResponse>(
    `/source-providers?${query.toString()}`,
  );
}
