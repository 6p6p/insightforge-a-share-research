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

/** text/html 且无可用外部 URL（source_url 为空）→ 原文按钮禁用。 */
const noUrlHtmlEvidence: EvidenceCitationResponse = {
  ...htmlEvidence,
  provenance: {
    ...(htmlEvidence.provenance as DocumentProvenance),
    source_url: '',
  },
};

/** application/pdf 且 locator.page_number 缺失 → PDF 正常打开，不带 #page。 */
const pdfNoPageEvidence: EvidenceCitationResponse = {
  ...documentEvidence,
  provenance: {
    ...documentProvenance,
    locator: {
      locator_type: 'pdf_page',
      block_ordinal: null,
      char_start: null,
      char_end: null,
      ordinal: null,
      tag: null,
      xpath: null,
      element_id: null,
      page_number: null,
      line_index: 5,
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
  it('evidence citation：渲染证据头 + claim relations + Document provenance + PDF 按钮（#page）', async () => {
    mocks.getEvidenceCitation.mockResolvedValue(documentEvidence);
    renderDrawer({ kind: 'evidence', evidenceCardId: 'ev-doc-1' });

    expect(await screen.findByText('营收同比增长 12%。')).toBeInTheDocument();
    // claim relations 保留 relation（supports + context）
    expect(await screen.findByText('supports')).toBeInTheDocument();
    expect(screen.getByText('context')).toBeInTheDocument();
    expect(screen.getByText('来源追溯（Document）')).toBeInTheDocument();
    expect(mocks.getEvidenceCitation).toHaveBeenCalledWith('t1', 'ev-doc-1');

    // PDF → 可点 anchor，href 指向后端 content 端点并带 #page=<locator.page_number>
    //（antd icon 的 aria-label 会混入 accessible name，故用文本 + closest 定位）
    const pdfButton = screen.getByText('打开原文 PDF').closest('a');
    expect(pdfButton).not.toBeNull();
    expect(pdfButton).toHaveAttribute(
      'href',
      `${API_BASE_URL}/source-records/src-1/content#page=2`,
    );
    expect(pdfButton).toHaveAttribute('target', '_blank');
    expect(pdfButton?.getAttribute('rel')).toContain('noopener');
    expect(pdfButton?.getAttribute('rel')).toContain('noreferrer');
  });

  it('PDF 且 locator.page_number 缺失 → 正常打开，不伪造 #page（Gate B）', async () => {
    mocks.getEvidenceCitation.mockResolvedValue(pdfNoPageEvidence);
    renderDrawer({ kind: 'evidence', evidenceCardId: 'ev-doc-1' });

    await screen.findByText('来源追溯（Document）');
    const pdfButton = screen.getByText('打开原文 PDF').closest('a');
    expect(pdfButton).toHaveAttribute('href', `${API_BASE_URL}/source-records/src-1/content`);
    expect(pdfButton?.getAttribute('href')).not.toContain('#page=');
  });

  it('HTML citation：context text / xpath 正常 + 「打开原始网页」= source_url（Gate A）', async () => {
    mocks.getEvidenceCitation.mockResolvedValue(htmlEvidence);
    renderDrawer({ kind: 'evidence', evidenceCardId: 'ev-doc-1' });

    await screen.findByText('来源追溯（Document）');
    // 引用上下文纯文本正常显示
    expect(screen.getByText(/…营收增长 12%…/)).toBeInTheDocument();
    // html_dom locator 包含 tag / ordinal / xpath
    expect(screen.getByText('HTML 节点 <p> #3 /html/body/p[3]')).toBeInTheDocument();
    // 「打开原始网页」指向外部 source_url，target/rel 安全
    const webButton = screen.getByText('打开原始网页').closest('a');
    expect(webButton).not.toBeNull();
    expect(webButton).toHaveAttribute('href', 'https://example.com/report');
    expect(webButton).toHaveAttribute('target', '_blank');
    expect(webButton?.getAttribute('rel')).toContain('noopener');
    expect(webButton?.getAttribute('rel')).toContain('noreferrer');
    // 不应有 PDF 链接 / 禁用按钮
    expect(screen.queryByText('打开原文 PDF')).not.toBeInTheDocument();
    expect(screen.queryByText('原文预览不可用')).not.toBeInTheDocument();
  });

  it('non-PDF 且无可用外部 URL → 原文按钮禁用（spec N），不调用归档内容 API', async () => {
    mocks.getEvidenceCitation.mockResolvedValue(noUrlHtmlEvidence);
    renderDrawer({ kind: 'evidence', evidenceCardId: 'ev-doc-1' });

    await screen.findByText('来源追溯（Document）');
    const disabled = screen.getByText('原文预览不可用').closest('button');
    expect(disabled).not.toBeNull();
    expect(disabled).toBeDisabled();
    // 不应渲染 PDF 链接 / 原始网页链接
    expect(screen.queryByText('打开原文 PDF')).not.toBeInTheDocument();
    expect(screen.queryByText('打开原始网页')).not.toBeInTheDocument();
    // 未触发任何对归档 content 端点的请求（本组件始终不请求它；这里只断言无 PDF 锚点）
    expect(document.querySelector(`a[href*="/source-records/"]`)).toBeNull();
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


const financialEvidence: EvidenceCitationResponse = {
  evidence: {
    evidence_card_id: 'ev-fin-1',
    statement: 'revenue（consolidated）报告期 2024-12-31 的数值为 362,012,554',
    quote_text: '营业收入（千元） 362,012,554 400,917,045',
    evidence_type: 'metric',
    origin_type: 'financial_extraction',
  },
  claim_relations: [],
  provenance: {
    origin_type: 'financial_extraction',
    source_id: 'src-fin-1',
    provider_key: 'eastmoney',
    provider_label: '东方财富（公告数据中心）',
    title: '宁德时代:2024年年度报告',
    source_url: 'https://pdf.dfcfw.com/xxx.pdf',
    published_at: '2025-03-14T00:00:00Z',
    authority_tier: 3,
    document_type: 'annual_report',
    raw_artifact_id: 'raw-fin-1',
    media_type: 'application/pdf',
    parsed_source_id: 'ps-fin-1',
    block_id: 'block-fin-1',
    locator: {
      locator_type: 'pdf_page',
      block_ordinal: null,
      char_start: null,
      char_end: null,
      ordinal: null,
      tag: null,
      xpath: null,
      element_id: null,
      page_number: 42,
      line_index: 7,
      bbox: null,
      page_width: null,
      page_height: null,
    },
    locator_refs: [],
    context_text: '营业收入（千元） 362,012,554 400,917,045 -9.70%',
    quote_text: '营业收入（千元） 362,012,554 400,917,045',
  },
};

it('financial_extraction provenance 渲染财务提取来源追溯', async () => {
  mocks.getEvidenceCitation.mockResolvedValue(financialEvidence);
  renderDrawer({ kind: 'evidence', evidenceCardId: 'ev-fin-1' });

  expect(await screen.findByText('来源追溯（财务提取）')).toBeInTheDocument();
  expect(screen.getByText('宁德时代:2024年年度报告')).toBeInTheDocument();
  expect(screen.getByText('第 42 页 · 第 7 行')).toBeInTheDocument();
  const pdfButton = screen.getByText('打开原文 PDF').closest('a');
  expect(pdfButton).not.toBeNull();
  expect(pdfButton).toHaveAttribute(
    'href',
    `${API_BASE_URL}/source-records/src-fin-1/content#page=42`,
  );
});
