/** 引用导航抽屉（Stage 6B.2 spec O/P + K/L）：Evidence / Claim citation 只读视图。

ReportTab 的观点 / 证据 Tag、EvidenceTab 的「查看引用」都通过页级
`CitationTarget` 打开本抽屉：
- `{ kind: 'evidence', evidenceCardId }` → `GET /tasks/{id}/citations/evidence/{card}`：
  evidence 头部 + canonical Claim relations + verified Document / Macro provenance；
- `{ kind: 'claim', claimId }` → `GET /tasks/{id}/citations/claims/{claim}`：
  claim 元数据 + evidence relations（relation 保留 supports / contradicts / context）。

「原文打开策略」（spec N + 6B.2 Gate A/B）：后端 content 端点只服务 PDF（其他
媒体类型 415）。PDF → 打开流式端点并带 `#page=<n>`（locator.page_number，前端不
重新计算）；非 PDF 且 source_url 为 http/https → 直接打开原始网页（target=_blank
+ rel=noopener noreferrer）；否则禁用提示，不请求不可解析的原始字节。绝不使用
dangerouslySetInnerHTML / 不内联渲染归档 HTML。

任何层都只读，绝不展示 fingerprint / storage_key / prompt / raw provider JSON。
 */

import { useQuery } from '@tanstack/react-query';
import {
  Alert,
  Button,
  Descriptions,
  Divider,
  Drawer,
  Empty,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import { FilePdfOutlined, LinkOutlined } from '@ant-design/icons';

import { API_BASE_URL } from '../../api/client';
import { getClaimCitation, getEvidenceCitation, taskKeys } from '../../api/tasks';
import {
  artifactErrorMessage,
  isIntegrityError,
} from '../artifacts/integrity';
import type {
  CitationLocator,
  CitationTarget,
  DocumentProvenance,
  EvidenceProvenance,
  MacroProvenance,
} from '../../types/citation';

export type { CitationTarget };

const { Text } = Typography;

const RELATION_COLOR: Record<string, string> = {
  supports: 'green',
  contradicts: 'red',
  context: 'default',
};

const ORIGIN_LABEL: Record<string, string> = {
  document_chunk: '文档证据',
  macro_observation: '宏观证据',
};

interface Props {
  taskId: string;
  target: CitationTarget | null;
  onClose: () => void;
  /** evidence 抽屉内点击某 claim relation → 导航到该 claim 的 citation。 */
  onNavigateClaim?: (claimId: string) => void;
  /** claim 抽屉内点击某 evidence relation → 导航到该 evidence 的 citation。 */
  onNavigateEvidence?: (evidenceCardId: string) => void;
}

/** 打开抽屉时应重置为哪个引用目标（target 非空时）。 */
export function isCitationOpen(target: CitationTarget | null): boolean {
  return target != null;
}

export function CitationDrawer({
  taskId,
  target,
  onClose,
  onNavigateClaim,
  onNavigateEvidence,
}: Props): React.JSX.Element {
  const evidenceCardId = target?.kind === 'evidence' ? target.evidenceCardId : null;
  const claimId = target?.kind === 'claim' ? target.claimId : null;

  const evidenceQuery = useQuery({
    queryKey: taskKeys.citationEvidence(taskId, evidenceCardId ?? ''),
    queryFn: () => getEvidenceCitation(taskId, evidenceCardId as string),
    enabled: evidenceCardId != null,
    retry: false,
  });

  const claimQuery = useQuery({
    queryKey: taskKeys.citationClaim(taskId, claimId ?? ''),
    queryFn: () => getClaimCitation(taskId, claimId as string),
    enabled: claimId != null,
    retry: false,
  });

  const open = isCitationOpen(target);

  return (
    <Drawer
      open={open}
      onClose={onClose}
      width={640}
      destroyOnClose
      title={evidenceCardId ? '证据引用' : claimId ? '观点引用' : '引用'}
    >
      {!open ? null : evidenceCardId != null ? (
        <EvidenceCitationBody
          isLoading={evidenceQuery.isLoading}
          isError={evidenceQuery.isError}
          error={evidenceQuery.error}
          data={evidenceQuery.data}
          onNavigateClaim={onNavigateClaim}
        />
      ) : claimId != null ? (
        <ClaimCitationBody
          isLoading={claimQuery.isLoading}
          isError={claimQuery.isError}
          error={claimQuery.error}
          data={claimQuery.data}
          onNavigateEvidence={onNavigateEvidence}
        />
      ) : (
        <Empty description="请选择一条证据或观点" />
      )}
    </Drawer>
  );
}

// ------------------------------------------------------------------ evidence citation

function EvidenceCitationBody({
  isLoading,
  isError,
  error,
  data,
  onNavigateClaim,
}: {
  isLoading: boolean;
  isError: boolean;
  error: unknown;
  data?: import('../../types/citation').EvidenceCitationResponse;
  onNavigateClaim?: (claimId: string) => void;
}): React.JSX.Element {
  if (isLoading) {
    return (
      <div style={{ textAlign: 'center', padding: 48 }}>
        <Spin tip="正在加载引用…" />
      </div>
    );
  }
  if (isError || !data) {
    return (
      <Alert
        type={isIntegrityError(error) ? 'error' : 'error'}
        showIcon
        message={artifactErrorMessage(error, '加载证据引用失败')}
      />
    );
  }

  const ev = data.evidence;
  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Descriptions size="small" column={1} bordered>
        <Descriptions.Item label="证据陈述">{ev.statement}</Descriptions.Item>
        <Descriptions.Item label="类型">{ev.evidence_type}</Descriptions.Item>
        <Descriptions.Item label="来源">
          {ORIGIN_LABEL[ev.origin_type] ?? ev.origin_type}
        </Descriptions.Item>
      </Descriptions>

      {ev.quote_text ? (
        <div style={{ background: '#f6f6f6', padding: 12, borderRadius: 6 }}>
          <Text type="secondary">引用原文</Text>
          <div style={{ marginTop: 4 }}>
            <Text>「{ev.quote_text}」</Text>
          </div>
        </div>
      ) : null}

      <div>
        <Text strong>引用该证据的观点（{data.claim_relations.length}）</Text>
        {data.claim_relations.length === 0 ? (
          <Text type="secondary"> — 该证据未被任何 canonical 观点引用。</Text>
        ) : (
          <Space direction="vertical" size={6} style={{ width: '100%', marginTop: 8 }}>
            {data.claim_relations.map((rel) => (
              <div
                key={`${rel.claim_id}:${rel.relation}`}
                style={{
                  border: '1px solid #f0f0f0',
                  borderRadius: 6,
                  padding: 8,
                  cursor: onNavigateClaim ? 'pointer' : 'default',
                }}
                onClick={() => onNavigateClaim?.(rel.claim_id)}
                role={onNavigateClaim ? 'button' : undefined}
                tabIndex={onNavigateClaim ? 0 : undefined}
                onKeyDown={(e) => {
                  if (onNavigateClaim && (e.key === 'Enter' || e.key === ' ')) {
                    onNavigateClaim(rel.claim_id);
                  }
                }}
              >
                <Tag color={RELATION_COLOR[rel.relation] ?? 'default'}>{rel.relation}</Tag>
                <Text>{rel.claim_statement || rel.claim_id.slice(0, 8)}</Text>
                {onNavigateClaim ? (
                  <Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
                    → 查看该观点
                  </Text>
                ) : null}
              </div>
            ))}
          </Space>
        )}
      </div>

      <Divider style={{ margin: '8px 0' }} />
      <ProvenanceBlock provenance={data.provenance} />
    </Space>
  );
}

// ------------------------------------------------------------------ provenance

function ProvenanceBlock({ provenance }: { provenance: EvidenceProvenance }): React.JSX.Element {
  if (provenance.origin_type === 'document_chunk') {
    return <DocumentProvenanceBlock provenance={provenance} />;
  }
  return <MacroProvenanceBlock provenance={provenance} />;
}

/** 前端只接受 http/https 外部 URL（杜绝 javascript:/data:/file:）。 */
function isHttpUrl(value: string | null | undefined): boolean {
  if (!value) {
    return false;
  }
  try {
    const parsed = new URL(value);
    return parsed.protocol === 'http:' || parsed.protocol === 'https:';
  } catch {
    return false;
  }
}

/** 原文打开策略（spec N + Gate A/B）：
- PDF → 后端 content 端点（`#page=<n>` 由后端 provenance 的 locator.page_number
  决定，前端不重新计算；无 page_number 则正常打开 PDF）；
- 非 PDF 且 source_url 为 http/https → 打开原始网页（target=_blank +
  rel=noopener noreferrer，不经过后端、不渲染归档 HTML）；
- 其余 → 禁用提示（不请求不可解析的原始字节）。
 */
function OriginalTextButton({
  sourceId,
  mediaType,
  sourceUrl,
  pageNumber,
}: {
  sourceId: string;
  mediaType: string;
  sourceUrl: string | null;
  pageNumber: number | null;
}): React.JSX.Element {
  if (mediaType === 'application/pdf') {
    const base = `${API_BASE_URL}/source-records/${sourceId}/content`;
    const url = pageNumber != null ? `${base}#page=${pageNumber}` : base;
    return (
      <Button
        size="small"
        type="primary"
        icon={<LinkOutlined />}
        href={url}
        target="_blank"
        rel="noopener noreferrer"
      >
        打开原文 PDF
      </Button>
    );
  }
  if (isHttpUrl(sourceUrl)) {
    return (
      <Button
        size="small"
        icon={<LinkOutlined />}
        href={sourceUrl as string}
        target="_blank"
        rel="noopener noreferrer"
      >
        打开原始网页
      </Button>
    );
  }
  return (
    <Tooltip title={`当前媒体类型（${mediaType}）暂不支持原文预览，仅支持 PDF`}>
      <Button size="small" icon={<FilePdfOutlined />} disabled>
        原文预览不可用
      </Button>
    </Tooltip>
  );
}

function locatorLabel(locator: CitationLocator | null): string {
  if (!locator) {
    return '—';
  }
  if (locator.locator_type === 'pdf_page') {
    const parts = [`第 ${locator.page_number ?? '?'} 页`];
    if (locator.line_index != null) {
      parts.push(`第 ${locator.line_index} 行`);
    }
    return parts.join(' · ');
  }
  const parts = ['HTML 节点'];
  if (locator.tag) {
    parts.push(`<${locator.tag}>`);
  }
  if (locator.ordinal != null) {
    parts.push(`#${locator.ordinal}`);
  }
  if (locator.xpath) {
    parts.push(locator.xpath);
  }
  return parts.join(' ');
}

function DocumentProvenanceBlock({ provenance }: { provenance: DocumentProvenance }): React.JSX.Element {
  return (
    <div>
      <Text strong>来源追溯（Document）</Text>
      <Descriptions size="small" column={1} bordered style={{ marginTop: 8 }}>
        <Descriptions.Item label="来源">
          {provenance.title || provenance.source_id.slice(0, 8)}
        </Descriptions.Item>
        <Descriptions.Item label="URL">
          {provenance.source_url ? (
            <a href={provenance.source_url} target="_blank" rel="noopener noreferrer">
              {provenance.source_url}
            </a>
          ) : (
            '—'
          )}
        </Descriptions.Item>
        <Descriptions.Item label="提供方">
          {provenance.provider_label}（{provenance.provider_key}）
        </Descriptions.Item>
        <Descriptions.Item label="发布/获取时间">{provenance.published_at ?? '—'}</Descriptions.Item>
        <Descriptions.Item label="文档类型">{provenance.document_type ?? '—'}</Descriptions.Item>
        <Descriptions.Item label="权威层级">{provenance.authority_tier}</Descriptions.Item>
        <Descriptions.Item label="定位">{locatorLabel(provenance.locator)}</Descriptions.Item>
        <Descriptions.Item label="Source ID">
          <Text code>{provenance.source_id}</Text>
        </Descriptions.Item>
      </Descriptions>
      <div style={{ marginTop: 8 }}>
        <OriginalTextButton
          sourceId={provenance.source_id}
          mediaType={provenance.media_type}
          sourceUrl={provenance.source_url}
          pageNumber={provenance.locator?.page_number ?? null}
        />
      </div>
      <div style={{ marginTop: 12 }}>
        <Text type="secondary">引用上下文（≤5000 字符）</Text>
        <pre
          style={{
            background: '#fafafa',
            padding: 12,
            borderRadius: 6,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            maxHeight: 260,
            overflow: 'auto',
            fontSize: 12,
          }}
        >
          {provenance.context_text}
        </pre>
      </div>
    </div>
  );
}

function MacroProvenanceBlock({ provenance }: { provenance: MacroProvenance }): React.JSX.Element {
  return (
    <div>
      <Text strong>来源追溯（Macro）</Text>
      <Descriptions size="small" column={1} bordered style={{ marginTop: 8 }}>
        <Descriptions.Item label="指标">{provenance.indicator}</Descriptions.Item>
        <Descriptions.Item label="地域">{provenance.geography}</Descriptions.Item>
        <Descriptions.Item label="观测期">{provenance.period}</Descriptions.Item>
        <Descriptions.Item label="观测值">
          {provenance.is_missing ? '缺失' : (provenance.value ?? '—')}
        </Descriptions.Item>
        <Descriptions.Item label="提供方">
          {provenance.provider_label}（{provenance.provider_key}）
        </Descriptions.Item>
        <Descriptions.Item label="权威层级">{provenance.authority_tier}</Descriptions.Item>
        <Descriptions.Item label="数据来源">
          {provenance.source_name ?? '—'}
          {provenance.source_organization ? ` / ${provenance.source_organization}` : ''}
        </Descriptions.Item>
        <Descriptions.Item label="抓取时间">{provenance.fetched_at ?? '—'}</Descriptions.Item>
        <Descriptions.Item label="系列 ID">
          <Text code>{provenance.series_id}</Text>
        </Descriptions.Item>
        <Descriptions.Item label="快照 ID">
          <Text code>{provenance.snapshot_id}</Text>
        </Descriptions.Item>
        <Descriptions.Item label="观测 ID">
          <Text code>{provenance.observation_id}</Text>
        </Descriptions.Item>
      </Descriptions>
      {provenance.artifact_links.length > 0 ? (
        <div style={{ marginTop: 8 }}>
          <Text type="secondary">归档产物</Text>
          <Space size={4} wrap style={{ marginTop: 4 }}>
            {provenance.artifact_links.map((link) => (
              <Tooltip key={link.artifact_id} title={`${link.media_type} · 第 ${link.page ?? '?'} 页`}>
                <Tag color="cyan">
                  {link.role}·{link.artifact_id.slice(0, 8)}
                </Tag>
              </Tooltip>
            ))}
          </Space>
        </div>
      ) : null}
    </div>
  );
}

// ------------------------------------------------------------------ claim citation

function ClaimCitationBody({
  isLoading,
  isError,
  error,
  data,
  onNavigateEvidence,
}: {
  isLoading: boolean;
  isError: boolean;
  error: unknown;
  data?: import('../../types/citation').ClaimCitationResponse;
  onNavigateEvidence?: (evidenceCardId: string) => void;
}): React.JSX.Element {
  if (isLoading) {
    return (
      <div style={{ textAlign: 'center', padding: 48 }}>
        <Spin tip="正在加载引用…" />
      </div>
    );
  }
  if (isError || !data) {
    return (
      <Alert
        type="error"
        showIcon
        message={artifactErrorMessage(error, '加载观点引用失败')}
      />
    );
  }

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Descriptions size="small" column={1} bordered>
        <Descriptions.Item label="观点陈述">{data.statement}</Descriptions.Item>
        <Descriptions.Item label="分析域">{data.domain}</Descriptions.Item>
        <Descriptions.Item label="观点类型">{data.kind}</Descriptions.Item>
        <Descriptions.Item label="置信度">
          <Tag color={data.confidence === 'high' ? 'green' : data.confidence === 'medium' ? 'blue' : 'default'}>
            {data.confidence}
          </Tag>
        </Descriptions.Item>
        <Descriptions.Item label="重要性">
          <Tag color={data.importance === 'high' ? 'volcano' : data.importance === 'medium' ? 'orange' : 'default'}>
            {data.importance}
          </Tag>
        </Descriptions.Item>
      </Descriptions>

      <div>
        <Text strong>支撑该观点的证据（{data.evidence_relations.length}）</Text>
        {data.evidence_relations.length === 0 ? (
          <Text type="secondary"> — 该观点未被关联到任何证据。</Text>
        ) : (
          <Space direction="vertical" size={6} style={{ width: '100%', marginTop: 8 }}>
            {data.evidence_relations.map((rel) => (
              <div
                key={`${rel.evidence_card_id}:${rel.relation}`}
                style={{
                  border: '1px solid #f0f0f0',
                  borderRadius: 6,
                  padding: 8,
                  cursor: onNavigateEvidence ? 'pointer' : 'default',
                }}
                onClick={() => onNavigateEvidence?.(rel.evidence_card_id)}
                role={onNavigateEvidence ? 'button' : undefined}
                tabIndex={onNavigateEvidence ? 0 : undefined}
                onKeyDown={(e) => {
                  if (onNavigateEvidence && (e.key === 'Enter' || e.key === ' ')) {
                    onNavigateEvidence(rel.evidence_card_id);
                  }
                }}
              >
                <Tag color={RELATION_COLOR[rel.relation] ?? 'default'}>{rel.relation}</Tag>
                <Text>{rel.evidence_statement || rel.evidence_card_id.slice(0, 8)}</Text>
                {onNavigateEvidence ? (
                  <Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
                    → 查看该证据
                  </Text>
                ) : null}
              </div>
            ))}
          </Space>
        )}
      </div>
    </Space>
  );
}
