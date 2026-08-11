import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';
import type { Mock } from 'vitest';

import { renderWithProviders } from '../../test/render';
import { API_BASE_URL } from '../../api/client';
import { ApiError } from '../../types/api';
import type {
  ClaimCitationResponse,
  DocumentProvenance,
  EvidenceCitationResponse,
} from '../../types/citation';
import { CitationDrawer } from './CitationDrawer';

const mocks = vi.hoisted(() => ({
  getEvidenceCitation: vi.fn(),
  getClaimCitation: vi.fn(),
}));

vi.mock('../../api/tasks', () => ({
  taskKeys: {
    all: ['tasks'],
    citationEvidence: (id: string, cid: string) => ['tasks', 'citations', id, 'evidence', cid],
    citationClaim: (id: string, cid: string) => ['tasks', 'citations', id, 'claims', cid],
  },
  getEvidenceCitation: mocks.getEvidenceCitation,
  getClaimCitation: mocks.getClaimCitation,
}));

const documentProvenance: DocumentProvenance = {
  origin_type: 'document_chunk',
  source_id: 'src-1',
  provider_key: 'xinhuanet',
  provider_label: '新华网',
  title: '贵州茅台 2026 半年报',
  source_url: 'https://example.com/report',
  published_at: '2026-08-01T00:00:00Z',
  authority_tier: 3,
  document_type: 'annual_report',
  raw_artifact_id: 'raw-1',
  media_type: 'application/pdf',
  parsed_source_id: 'ps-1',
  chunk_id: 'chunk-1',
  locator: {
    locator_type: 'pdf_page',
    block_ordinal: null,
    char_start: null,
    char_end: null,
    ordinal: null,
    tag: null,
    xpath: null,
    element_id: null,
    page_number: 2,
    line_index: 5,
    bbox: null,
    page_width: null,
    page_height: null,
  },
  locator_refs: [],
  context_text: '…营收增长 12%…',
  quote_text: '营收增长 12%',
};

const documentEvidence: EvidenceCitationResponse = {
  evidence: {
    evidence_card_id: 'ev-doc-1',
    statement: '营收同比增长 12%。',
    quote_text: '营收增长 12%',
    evidence_type: 'financial',
    origin_type: 'document_chunk',
  },
  claim_relations: [
    { claim_id: 'cl-1', claim_statement: '2026 年营收增长与行业均值相符。', relation: 'supports' },
    { claim_id: 'cl-2', claim_statement: '需关注季节波动。', relation: 'context' },
  ],
  provenance: documentProvenance,
};

const htmlEvidence: EvidenceCitationResponse = {
  ...documentEvidence,
  provenance: {
    ...documentProvenance,
    media_type: 'text/html',
    locator: {
      locator_type: 'html_dom',
      block_ordinal: null,
      char_start: null,
      char_end: null,
      ordinal: 3,
      tag: 'p',
      xpath: '/html/body/p[3]',
      element_id: null,
      page_number: null,
      line_index: null,
      bbox: null,
      page_width: null,
      page_height: null,
    },
  },
};

const macroEvidence: EvidenceCitationResponse = {
  evidence: {
    evidence_card_id: 'ev-macro-1',
    statement: 'CPI 同比 +0.2%。',
    quote_text: null,
    evidence_type: 'macro',
    origin_type: 'macro_observation',
  },
  claim_relations: [
    { claim_id: 'cl-3', claim_statement: '通胀温和，利于消费板块。', relation: 'supports' },
  ],
  provenance: {
    origin_type: 'macro_observation',
    observation_id: 'obs-1',
    period: '2026-07',
    value: '0.2',
    is_missing: false,
    snapshot_id: 'snap-1',
    fetched_at: '2026-08-01T00:00:00Z',
    series_id: 'series-1',
    indicator: 'CPI 同比',
    geography: 'CN',
    provider_key: 'nbs',
    provider_label: '国家统计局',
    authority_tier: 1,
    source_name: '国家数据',
    source_organization: '国家统计局',
    raw_artifact_id: 'raw-m-1',
    media_type: 'text/csv',
    artifact_links: [{ role: 'raw', page: null, artifact_id: 'raw-m-1', media_type: 'text/csv', fetched_at: '2026-08-01T00:00:00Z' }],
  },
};

const claimCitation: ClaimCitationResponse = {
  claim_id: 'cl-1',
  statement: '2026 年营收增长与行业均值相符。',
  domain: 'business',
  kind: 'fact',
  confidence: 'high',
  importance: 'high',
  evidence_relations: [
    { evidence_card_id: 'ev-doc-1', evidence_statement: '营收同比增长 12%。', relation: 'supports' },
  ],
};

function renderDrawer(target: { kind: 'evidence'; evidenceCardId: string } | { kind: 'claim'; claimId: string } | null, extra?: { onNavigateClaim?: Mock; onNavigateEvidence?: Mock }): ReturnType<typeof renderWithProviders> {
  return renderWithProviders(
    <CitationDrawer
      taskId="t1"
      target={target}
      onClose={() => {}}
      onNavigateClaim={extra?.onNavigateClaim}
      onNavigateEvidence={extra?.onNavigateEvidence}
    />,
  );
}

beforeEach(() => {
  mocks.getEvidenceCitation.mockReset();
  mocks.getClaimCitation.mockReset();
});

describe('CitationDrawer（Stage 6B.2 spec K/L/N）', () => {
  it('evidence citation：渲染证据头 + claim relations + Document provenance + PDF 按钮', async () => {
    mocks.getEvidenceCitation.mockResolvedValue(documentEvidence);
    renderDrawer({ kind: 'evidence', evidenceCardId: 'ev-doc-1' });

    expect(await screen.findByText('营收同比增长 12%。')).toBeInTheDocument();
    // claim relations 保留 relation（supports + context）
    expect(await screen.findByText('supports')).toBeInTheDocument();
    expect(screen.getByText('context')).toBeInTheDocument();
    expect(screen.getByText('来源追溯（Document）')).toBeInTheDocument();
    expect(mocks.getEvidenceCitation).toHaveBeenCalledWith('t1', 'ev-doc-1');

    // PDF → 可点 anchor，href 指向后端 content 端点（antd icon 的 aria-label 会混入
    // accessible name，故用文本 + closest 定位 anchor 而非 getByRole）
    const pdfButton = screen.getByText('打开原文 PDF').closest('a');
    expect(pdfButton).not.toBeNull();
    expect(pdfButton).toHaveAttribute('href', `${API_BASE_URL}/source-records/src-1/content`);
  });

  it('non-PDF 媒体类型 → 原文按钮禁用（spec N）', async () => {
    mocks.getEvidenceCitation.mockResolvedValue(htmlEvidence);
    renderDrawer({ kind: 'evidence', evidenceCardId: 'ev-doc-1' });

    await screen.findByText('来源追溯（Document）');
    const disabled = screen.getByText('原文预览不可用').closest('button');
    expect(disabled).not.toBeNull();
    expect(disabled).toBeDisabled();
    // 不应渲染 PDF 链接
    expect(screen.queryByText('打开原文 PDF')).not.toBeInTheDocument();
  });

  it('macro evidence citation：渲染 Macro provenance（指标/系列/归档产物）', async () => {
    mocks.getEvidenceCitation.mockResolvedValue(macroEvidence);
    renderDrawer({ kind: 'evidence', evidenceCardId: 'ev-macro-1' });

    expect(await screen.findByText('CPI 同比 +0.2%。')).toBeInTheDocument();
    expect(screen.getByText('来源追溯（Macro）')).toBeInTheDocument();
    expect(screen.getByText('CPI 同比')).toBeInTheDocument();
    // 提供方渲染为 label（key）：provider_label（provider_key）
    expect(screen.getByText('国家统计局（nbs）')).toBeInTheDocument();
    // 归档产物链接
    expect(await screen.findByText('raw·raw-m-1')).toBeInTheDocument();
  });

  it('claim citation：渲染 claim 元数据 + evidence relations', async () => {
    mocks.getClaimCitation.mockResolvedValue(claimCitation);
    renderDrawer({ kind: 'claim', claimId: 'cl-1' });

    expect(await screen.findByText('支撑该观点的证据（1）')).toBeInTheDocument();
    expect(screen.getByText('2026 年营收增长与行业均值相符。')).toBeInTheDocument();
    expect(mocks.getClaimCitation).toHaveBeenCalledWith('t1', 'cl-1');
  });

  it('integrity 错误（409）→ 显示「产物完整性校验失败」', async () => {
    mocks.getEvidenceCitation.mockRejectedValue(
      new ApiError(409, 'task_artifact_integrity', '任务产物完整性校验失败', 'req-1'),
    );
    renderDrawer({ kind: 'evidence', evidenceCardId: 'ev-doc-1' });

    expect(await screen.findByText('产物完整性校验失败')).toBeInTheDocument();
  });

  it('点击 claim relation → onNavigateClaim(claimId)', async () => {
    mocks.getEvidenceCitation.mockResolvedValue(documentEvidence);
    const onNavigateClaim = vi.fn();
    renderDrawer({ kind: 'evidence', evidenceCardId: 'ev-doc-1' }, { onNavigateClaim });

    await screen.findByText('2026 年营收增长与行业均值相符。');
    fireEvent.click(screen.getByText('2026 年营收增长与行业均值相符。'));
    expect(onNavigateClaim).toHaveBeenCalledWith('cl-1');
  });
});
