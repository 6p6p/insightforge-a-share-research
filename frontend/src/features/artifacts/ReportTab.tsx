/** 任务最新报告投影（verify_report_integrity read-side，stage 6B.1 spec I）。

展示真实正文：sections[].paragraphs[].{text, claim_ids, evidence_card_ids,
conflict_indexes, evidence_gap_indexes}。section_id 是 outline 符号键（如 "S2"），
与审核 issue 的 section_id 关联。

Stage 6B.2（spec O/Q + Final Gate C/D）：段落里的「观点」「证据」Tag 可点击 →
打开 CitationDrawer（evidence / claim citation）；ReviewsTab「定位报告」通过
`locateSection`/`locateParagraph` 传进来 → 高亮并滚动。定位规则：
- section + paragraph → 定位段落（`report-para-<section_id>:<index>`）；
- section only → 定位整节容器（`report-section-<section_id>`），不伪造 paragraph；
- 目标不存在 → 轻量警告「未找到对应报告位置，报告版本可能已变化。」（不静默）。
 */

import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Alert, Card, Descriptions, Divider, Space, Tag, Typography } from 'antd';

import { getTaskReport, taskKeys } from '../../api/tasks';
import type {
  ReportArtifactResponse,
  ReportSectionArtifact,
} from '../../types/artifacts';
import type { CitationTarget } from '../../types/citation';
import { artifactErrorMessage } from './integrity';

const { Text } = Typography;

const SECTION_TYPE_LABEL: Record<string, string> = {
  overview: '概览',
  narrative: '叙事',
  analysis: '分析',
  risk: '风险',
  valuation: '估值',
  conclusion: '结论',
};

interface Props {
  taskId: string;
  /** 点击观点/证据 Tag → 打开对应 citation。 */
  onOpenCitation?: (target: CitationTarget) => void;
  /** ReviewsTab「定位报告」：高亮并滚动到该 section/paragraph。 */
  locateSection?: string | null;
  locateParagraph?: number | null;
}

/** 定位目标：`section + paragraph` → 段落；`section only` → 整节容器。 */
type LocateTarget =
  | { kind: 'paragraph'; key: string }
  | { kind: 'section'; key: string }
  | null;

/** 从 URL 定位参数解析定位目标（不做 null → -1 伪造）。 */
function locateTargetOf(
  locateSection: string | null | undefined,
  locateParagraph: number | null | undefined,
): LocateTarget {
  if (!locateSection) {
    return null;
  }
  if (locateParagraph != null) {
    return { kind: 'paragraph', key: `${locateSection}:${locateParagraph}` };
  }
  return { kind: 'section', key: locateSection };
}

export function ReportTab({
  taskId,
  onOpenCitation,
  locateSection,
  locateParagraph,
}: Props): React.JSX.Element {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: taskKeys.report(taskId),
    queryFn: () => getTaskReport(taskId),
    refetchInterval: 5000,
  });

  const locateTarget = locateTargetOf(locateSection, locateParagraph);

  if (isError) {
    return <Alert type="error" showIcon message={artifactErrorMessage(error, '加载报告失败')} />;
  }
  if (isLoading || !data) {
    return <Alert type="info" showIcon message="正在加载报告…" />;
  }
  if (!data.report_id) {
    return <Alert type="info" showIcon message="该任务尚无报告（未执行 Stage 5 或审核未通过）。" />;
  }
  return (
    <ReportContent
      data={data}
      onOpenCitation={onOpenCitation}
      locateTarget={locateTarget}
    />
  );
}

function ReportContent({
  data,
  onOpenCitation,
  locateTarget,
}: {
  data: ReportArtifactResponse;
  onOpenCitation?: (target: CitationTarget) => void;
  locateTarget: LocateTarget;
}): React.JSX.Element {
  const [located, setLocated] = useState<LocateTarget>(null);
  const [locateMissing, setLocateMissing] = useState(false);

  // URL 定位参数到达时记录目标；数据/重取变化时重新定位。
  useEffect(() => {
    setLocated(locateTarget);
  }, [locateTarget]);

  // 数据就绪后：存在 → 高亮 + 平滑滚动；不存在 → 轻量警告（不静默、不自动替换）。
  useEffect(() => {
    if (!located) {
      return;
    }
    setLocateMissing(false);
    const elementId =
      located.kind === 'paragraph' ? `report-para-${located.key}` : `report-section-${located.key}`;
    const el = document.getElementById(elementId);
    if (el && typeof el.scrollIntoView === 'function') {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    } else {
      setLocateMissing(true);
    }
  }, [located, data.report_id]);

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      {locateMissing ? (
        <Alert type="warning" showIcon message="未找到对应报告位置，报告版本可能已变化。" />
      ) : null}
      <Card title="报告概览">
        <Descriptions size="small" column={2}>
          <Descriptions.Item label="报告 ID">
            <Text code>{data.report_id}</Text>
          </Descriptions.Item>
          <Descriptions.Item label="大纲 ID">
            {data.outline_id ? <Text code>{data.outline_id}</Text> : '—'}
          </Descriptions.Item>
          <Descriptions.Item label="分析基准日">{data.analysis_as_of ?? '—'}</Descriptions.Item>
          <Descriptions.Item label="Schema 版本">{data.report_schema_version ?? '—'}</Descriptions.Item>
          <Descriptions.Item label="章节数">{data.section_count ?? '—'}</Descriptions.Item>
          <Descriptions.Item label="报告指纹">
            {data.report_fingerprint ? <Text code>{data.report_fingerprint}</Text> : '—'}
          </Descriptions.Item>
        </Descriptions>
      </Card>
      {data.sections.length > 0 ? (
        <Card title={`报告正文（${data.sections.length} 节）`}>
          {data.sections.map((section, index) => (
            <SectionBlock
              key={section.section_id}
              section={section}
              last={index === data.sections.length - 1}
              onOpenCitation={onOpenCitation}
              located={located}
            />
          ))}
        </Card>
      ) : (
        <Alert type="info" showIcon message="该报告尚无正文段落。" />
      )}
    </Space>
  );
}

function SectionBlock({
  section,
  last,
  onOpenCitation,
  located,
}: {
  section: ReportSectionArtifact;
  last: boolean;
  onOpenCitation?: (target: CitationTarget) => void;
  located: LocateTarget;
}): React.JSX.Element {
  const isSectionLocated = located?.kind === 'section' && located.key === section.section_id;
  return (
    <div
      id={`report-section-${section.section_id}`}
      style={
        isSectionLocated
          ? {
              background: '#fffbe6',
              border: '1px solid #faad14',
              borderRadius: 6,
              padding: 12,
            }
          : undefined
      }
    >
      <Space wrap size="small" style={{ marginBottom: 8 }}>
        <Text strong>{section.title}</Text>
        <Tag>{section.section_id}</Tag>
        <Tag color="blue">{SECTION_TYPE_LABEL[section.section_type] ?? section.section_type}</Tag>
      </Space>
      {section.paragraphs.length === 0 ? (
        <Text type="secondary">（无正文段落）</Text>
      ) : (
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          {section.paragraphs.map((paragraph) => {
            const key = `${section.section_id}:${paragraph.paragraph_index}`;
            const isLocated =
              located?.kind === 'paragraph' && located.key === key;
            return (
              <div
                key={paragraph.paragraph_index}
                id={`report-para-${key}`}
                style={
                  isLocated
                    ? {
                        background: '#fffbe6',
                        border: '1px solid #faad14',
                        borderRadius: 6,
                        padding: 8,
                      }
                    : undefined
                }
              >
                <Text>{paragraph.text}</Text>
                <ParagraphMeta paragraph={paragraph} onOpenCitation={onOpenCitation} />
              </div>
            );
          })}
        </Space>
      )}
      {!last ? <Divider style={{ margin: '16px 0' }} /> : null}
    </div>
  );
}

interface ParagraphMetaProps {
  paragraph: {
    claim_ids: string[];
    evidence_card_ids: string[];
    conflict_indexes: number[];
    evidence_gap_indexes: number[];
  };
  onOpenCitation?: (target: CitationTarget) => void;
}

function ParagraphMeta({ paragraph, onOpenCitation }: ParagraphMetaProps): React.JSX.Element {
  const tags: React.JSX.Element[] = [];
  paragraph.claim_ids.forEach((claimId) =>
    tags.push(
      <Tag
        key={`c-${claimId}`}
        color="blue"
        style={onOpenCitation ? { cursor: 'pointer' } : undefined}
        onClick={onOpenCitation ? () => onOpenCitation({ kind: 'claim', claimId }) : undefined}
        title={onOpenCitation ? '查看该观点引用' : undefined}
      >
        观点 {claimId.slice(0, 8)}
      </Tag>,
    ),
  );
  paragraph.evidence_card_ids.forEach((cardId) =>
    tags.push(
      <Tag
        key={`e-${cardId}`}
        style={onOpenCitation ? { cursor: 'pointer' } : undefined}
        onClick={onOpenCitation ? () => onOpenCitation({ kind: 'evidence', evidenceCardId: cardId }) : undefined}
        title={onOpenCitation ? '查看该证据引用' : undefined}
      >
        证据 {cardId.slice(0, 8)}
      </Tag>,
    ),
  );
  paragraph.conflict_indexes.forEach((idx) =>
    tags.push(<Tag key={`x-${idx}`} color="orange">冲突 #{idx}</Tag>),
  );
  paragraph.evidence_gap_indexes.forEach((idx) =>
    tags.push(<Tag key={`g-${idx}`} color="volcano">缺口 #{idx}</Tag>),
  );
  if (tags.length === 0) {
    return <div />;
  }
  return (
    <div style={{ marginTop: 4 }}>
      <Space size={4} wrap>
        {tags}
      </Space>
    </div>
  );
}
