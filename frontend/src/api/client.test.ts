import { describe, expect, it } from 'vitest';

import { DEFAULT_API_BASE_URL } from './client';

describe('API base URL（Gate A：frontend/backend 端口对齐）', () => {
  it('开发默认后端地址必须为 8001（与 backend Settings.app_port 一致）', () => {
    expect(DEFAULT_API_BASE_URL).toBe('http://localhost:8001/api/v1');
  });

  it('默认 base URL 不含通配符来源，使用显式 localhost 端口', () => {
    expect(DEFAULT_API_BASE_URL).not.toContain('*');
    expect(DEFAULT_API_BASE_URL).toContain('localhost:8001');
  });
});
