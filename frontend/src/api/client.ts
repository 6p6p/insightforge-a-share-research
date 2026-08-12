/** 集中式 API client（spec I）。组件不直接散落 fetch URL。

- `VITE_API_BASE_URL` 来自 env（默认 http://localhost:8001/api/v1，与后端
  Settings.app_port=8001 对齐）。
- 统一处理：JSON 错误信封、request_id、409、422（FastAPI validation）。
 */

import { ApiError, type ErrorEnvelope, type ValidationDetail } from '../types/api';

/** 开发默认后端地址：必须与 backend/app/core/config.py `app_port=8001` 对齐。 */
export const DEFAULT_API_BASE_URL = 'http://localhost:8001/api/v1';

export const API_BASE_URL: string =
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? DEFAULT_API_BASE_URL;

export interface RequestOptions {
  method?: string;
  body?: unknown;
  headers?: Record<string, string>;
  /** 非 JSON 响应（如 SSE）时不抛错直接返回文本。 */
  raw?: boolean;
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, headers = {}, raw = false } = options;
  const url = path.startsWith('http') ? path : `${API_BASE_URL}${path}`;

  const init: RequestInit = { method, headers: { ...headers } };
  if (body !== undefined) {
    if (body instanceof FormData) {
      // multipart/form-data：不手动设 Content-Type，fetch 会带上 boundary。
      init.body = body;
    } else {
      init.headers = { ...init.headers, 'Content-Type': 'application/json' };
      init.body = JSON.stringify(body);
    }
  }

  const response = await fetch(url, init);

  if (raw) {
    return (await response.text()) as T;
  }

  const text = await response.text();
  const data = text ? safeParse(text) : null;

  if (!response.ok) {
    throw parseError(response.status, data);
  }
  return data as T;
}

function safeParse(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function parseError(status: number, data: unknown): ApiError {
  // 统一信封：{ error: { code, message, request_id } }
  const envelope = data as ErrorEnvelope | null;
  if (envelope?.error && typeof envelope.error.code === 'string') {
    return new ApiError(
      status,
      envelope.error.code,
      envelope.error.message,
      envelope.error.request_id,
    );
  }
  // FastAPI 校验 422：{ detail: [{ loc, msg, type }] }
  if (Array.isArray(data)) {
    const validation = data as ValidationDetail;
    const first = validation[0];
    const message = first ? `请求校验失败：${first.msg}（${first.loc.join('.')}）` : '请求校验失败';
    return new ApiError(status, 'validation_error', message, '', validation);
  }
  return new ApiError(status, 'http_error', `请求失败（HTTP ${status}）`, '');
}
