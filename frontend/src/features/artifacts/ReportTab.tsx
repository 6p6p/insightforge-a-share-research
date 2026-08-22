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
import { useMutation, useQuery } from '@tanstack/react-query';
import { Alert, Button, Card, Descriptions, Divider, Dropdown, List, Space, Tag, Typography } from 'antd';
import { DownloadOutlined } from '@ant-design/icons';

import { createExport, downloadExportContent, getTaskReport, getTaskReviews, taskKeys } from '../../api/tasks';
import { ApiError } from '../../types/api';
import type {
  ReportArtifactResponse,
  ReportSectionArtifact,
  ReviewIssueArtifactResponse,
} from '../../types/artifacts';
import type { CitationTarget } from '../../types/citation';
import type { ExportFormat } from '../../types/export';
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

/** 导出格式下拉项（stage 6C spec Q）。 */
const EXPORT_FORMAT_ITEMS: { key: ExportFormat; label: string }[] = [
  { key: 'markdown', label: 'Markdown（.md）' },
  { key: 'docx', label: 'Word（.docx）' },
  { key: 'pdf', label: 'PDF（.pdf）' },
];

/** 「导出报告」Dropdown（spec Q）：POST 创建/replay → 下载 content 字节。
 * 409（不可导出 / 校验失败）→ 内联警告，不假装成功。 */
function ExportMenu({ taskId }: { taskId: string }): React.JSX.Element {
  const [exportError, setExportError] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: async (format: ExportFormat) => {
      const created = await createExport(taskId, format);
      const { blob, fileName } = await downloadExportContent(taskId, created.export_id);
      return { blob, fileName };
    },
    onSuccess: ({ blob, fileName }) => {
      setExportError(null);
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = fileName;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
    },
    onError: (error: unknown) => {
      if (error instanceof ApiError) {
        if (error.isConflict) {
          setExportError('报告当前不可导出（审核未通过 / 校验未通过 / 仍在运行）。');
        } else {
          setExportError(error.message);
        }
      } else {
        setExportError('导出失败，请稍后重试。');
      }
    },
  });

  const items = EXPORT_FORMAT_ITEMS.map((item) => ({
    key: item.key,
    label: item.label,
  }));

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="small">
      {exportError ? (
        <Alert type="error" showIcon message="导出失败" description={exportError} />
      ) : null}
      <Dropdown
        menu={{
          items,
          onClick: ({ key }) => mutation.mutate(key as ExportFormat),
        }}
        trigger={['click']}
        disabled={mutation.isPending}
      >
        <Button icon={<DownloadOutlined />} loading={mutation.isPending}>
          导出报告
        </Button>
      </Dropdown>
    </Space>
  );
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
    return <Alert type="info" showIcon message="该任务尚无报告（报告尚未生成或审核未通过）。" />;
  }
  return (
    <ReportContent
      taskId={taskId}
      data={data}
      onOpenCitation={onOpenCitation}
      locateTarget={locateTarget}
    />
  );
}

function ReportContent({
  taskId,
  data,
  onOpenCitation,
  locateTarget,
}: {
  taskId: string;
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
      <ExportMenu taskId={taskId} />
      <Card title="报告概览">
        <Descriptions size="small" column={2}>
          <Descriptions.Item label="分析基准日">{data.analysis_as_of ?? '—'}</Descriptions.Item>
          <Descriptions.Item label="章节数">{data.section_count ?? '—'}</Descriptions.Item>
        </Descriptions>
      </Card>
      <AuditReminder taskId={taskId} />
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

// ------------------------------------------------------------------ 审核提醒区域（v1.2.5）

/** issue_type → 产品化「问题描述」前缀（提醒 ≠ 失败原因）。 */
const ISSUE_TYPE_HINT: Record<string, string> = {
  unsupported_by_evidence: '该结论在现有证据中没有足够支撑',
  stale_or_temporally_misaligned: '证据时效或时间对齐需要关注',
  evidence_mismatch: '证据与主张存在错配，请人工核对',
  claim_misrepresentation: '主张表述与证据原意可能不一致',
  weak_source_quality: '证据来源质量一般',
  omitted_counterevidence: '缺少反证讨论',
  causal_overreach: '因果推断可能过度',
  valuation_overreach: '估值推断可能过度',
  insufficient_evidence: '证据充分性不足',
  unresolved_conflict: '冲突信息未完全解决',
  wording_overclaim: '措辞存在过度声明',
};

/** severity -> 产品等级标签（v1.2.5 风险提示语义）。 */
const SEVERITY_LEVEL_LABEL: Record<string, { label: string; color: string }> = {
  critical: { label: '重要审核提醒', color: 'warning' },
  warning: { label: '审核提醒', color: 'gold' },
  info: { label: '参考提示', color: 'blue' },
};

function AuditReminder({ taskId }: { taskId: string }): React.JSX.Element | null {
  const { data, isLoading } = useQuery({
    queryKey: taskKeys.reviews(taskId),
    queryFn: () => getTaskReviews(taskId),
    refetchInterval: 5000,
  });

  if (isLoading || !data) {
    return null;
  }
  const issues = data.issues ?? [];
  if (issues.length === 0 && (data.check?.findings?.length ?? 0) === 0) {
    return null;
  }

  const reminders: {
    key: string;
    level: string;
    section: string | null;
    message: string;
    suggestion: string;
  }[] = [];

  issues.forEach((issue: ReviewIssueArtifactResponse) => {
    const cfg = SEVERITY_LEVEL_LABEL[issue.severity] ?? SEVERITY_LEVEL_LABEL.warning;
    const hint = ISSUE_TYPE_HINT[issue.issue_type] ?? '';
    reminders.push({
      key: `audit-${issue.review_issue_id}`,
      level: cfg.label,
      section: issue.section_id,
      message: issue.message,
      suggestion: hint,
    });
  });

  data.check?.findings?.forEach((finding, index) => {
    reminders.push({
      key: `check-${finding.code}-${index}`,
      level: '审核提醒',
      section: finding.section_id,
      message: `确定性检查：${finding.code}`,
      suggestion: '请结合审核详情确认相关数据。',
    });
  });

  // 提醒不是失败原因：报告仍可交付，用户决定是否接受。
  return (
    <Card
      title="审核提醒"
      size="small"
      extra={<Tag color="gold">提醒 ≠ 失败原因</Tag>}
    >
      <Alert
        type="warning"
        showIcon
        message="报告期间发现以下审核提醒，请结合审核详情判断是否需要处理后重新研究，或直接接受当前报告。"
      />
      <List
        size="small"
        dataSource={reminders}
        renderItem={(item) => (
          <List.Item>
            <Space direction="vertical" size={2} style={{ width: '100%' }}>
              <Space size={4} wrap>
                <Tag color={item.level === '重要审核提醒' ? 'warning' : 'gold'}>
                  {item.level}
                </Tag>
                {item.section ? <Tag>章节：{item.section}</Tag> : null}
              </Space>
              <Text>{item.message}</Text>
              {item.suggestion ? <Text type="secondary">建议：{item.suggestion}</Text> : null}
            </Space>
          </List.Item>
        )}
      />
    </Card>
  );
}

