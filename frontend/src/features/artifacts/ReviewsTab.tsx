/** 任务最新审核视图（stage 6B.1 spec J 分层投影）。

Agent Audit 摘要保留在顶层（audit_status / recommended_route / issues）；
Deterministic Check / ReviewAction / Human Review / Research Backflow 各自
独立 layer，缺失为 null。所有层只读，绝不展示 prompt / raw provider response。
 */

import { useQuery } from '@tanstack/react-query';
import { Alert, Button, Card, Descriptions, Space, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';

import { getTaskReviews, taskKeys } from '../../api/tasks';
import type {
  CheckFindingArtifact,
  ReviewIssueArtifactResponse,
  ReviewsArtifactResponse,
} from '../../types/artifacts';
import { artifactErrorMessage } from './integrity';

const { Text } = Typography;

const SEVERITY_COLOR: Record<string, string> = {
  critical: 'red',
  major: 'orange',
  minor: 'blue',
  info: 'default',
};

const ROUTE_LABEL: Record<string, string> = {
  pass: '通过',
  approve: '批准',
  human_review: '人工审核',
  rewrite: '重写',
  research: '补充研究',
};

const CHECK_STATUS_COLOR: Record<string, string> = {
  pass: 'green',
  fail: 'red',
};

const ACTION_TYPE_COLOR: Record<string, string> = {
  rewrite: 'orange',
  request_human_review: 'blue',
  research: 'purple',
};

/** 审核状态 / 动作类型 → 产品语义（V1.1 不暴露内部枚举）。 */
const AUDIT_STATUS_LABEL: Record<string, string> = {
  pass: '通过',
  fail: '未通过',
  human_review: '待人工审核',
  pending: '待审核',
};

const ACTION_TYPE_LABEL: Record<string, string> = {
  rewrite: '要求重写',
  request_human_review: '请求人工审核',
  research: '需要补充研究',
};

const HUMAN_DECISION_COLOR: Record<string, string> = {
  approve: 'green',
  reject: 'red',
};

interface Props {
  taskId: string;
  /** 「定位报告」→ 切到报告 tab 并滚动到该 section/paragraph。 */
  onLocateReport?: (sectionId: string, paragraphIndex: number | null) => void;
}

export function ReviewsTab({ taskId, onLocateReport }: Props): React.JSX.Element {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: taskKeys.reviews(taskId),
    queryFn: () => getTaskReviews(taskId),
    refetchInterval: 5000,
  });

  if (isError) {
    return <Alert type="error" showIcon message={artifactErrorMessage(error, '加载审核视图失败')} />;
  }
  if (isLoading || !data) {
    return <Alert type="info" showIcon message="正在加载审核视图…" />;
  }
  if (!data.audit_id) {
    return <Alert type="info" showIcon message="该任务尚无审核记录（报告审核尚未完成）。" />;
  }
  return <ReviewsContent data={data} onLocateReport={onLocateReport} />;
}

function ReviewsContent({
  data,
  onLocateReport,
}: {
  data: ReviewsArtifactResponse;
  onLocateReport?: (sectionId: string, paragraphIndex: number | null) => void;
}): React.JSX.Element {
  const issueColumns: ColumnsType<ReviewIssueArtifactResponse> = [
    { title: '序号', dataIndex: 'ordinal', width: 64 },
    {
      title: '严重度',
      dataIndex: 'severity',
      width: 100,
      render: (v: string) => <Tag color={SEVERITY_COLOR[v] ?? 'default'}>{v}</Tag>,
    },
    { title: '类型', dataIndex: 'issue_type', width: 160 },
    { title: '章节', dataIndex: 'section_id', width: 120 },
    { title: '段落', dataIndex: 'paragraph_index', width: 64, render: (v: number | null) => v ?? '—' },
    { title: '问题描述', dataIndex: 'message', ellipsis: true },
    ...(onLocateReport
      ? [
          {
            title: '定位',
            width: 90,
            render: (_: unknown, row: ReviewIssueArtifactResponse) => (
              <Button
                size="small"
                type="link"
                disabled={!row.section_id}
                onClick={() => onLocateReport(row.section_id, row.paragraph_index)}
              >
                定位报告
              </Button>
            ),
          },
        ]
      : []),
  ];

  const checkColumns: ColumnsType<CheckFindingArtifact> = [
    { title: '代码', dataIndex: 'code', width: 160, render: (v: string) => <Text code>{v}</Text> },
    { title: '章节', dataIndex: 'section_id', width: 100, render: (v: string | null) => v ?? '—' },
    { title: '段落', dataIndex: 'paragraph_index', width: 64, render: (v: number | null) => v ?? '—' },
    { title: '关联观点', dataIndex: 'related_claim_ids', render: (v: string[]) => v.length },
    { title: '关联证据', dataIndex: 'related_evidence_card_ids', render: (v: string[]) => v.length },
  ];

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Card title="审核概览">
        <Descriptions size="small" column={2}>
          <Descriptions.Item label="审核状态">
            {data.audit_status ? (AUDIT_STATUS_LABEL[data.audit_status] ?? data.audit_status) : '—'}
          </Descriptions.Item>
          <Descriptions.Item label="建议路由">
            {data.recommended_route
              ? (ROUTE_LABEL[data.recommended_route] ?? data.recommended_route)
              : '—'}
          </Descriptions.Item>
          <Descriptions.Item label="问题数">{data.issue_count}</Descriptions.Item>
        </Descriptions>
      </Card>
      <Card title={`审核问题（${data.issue_count}）`}>
        <Table<ReviewIssueArtifactResponse>
          rowKey="review_issue_id"
          dataSource={data.issues}
          columns={issueColumns}
          pagination={false}
          locale={{ emptyText: '无审核问题' }}
          size="small"
        />
      </Card>
      <Card title="确定性检查">
        {data.check ? (
          <Space direction="vertical" style={{ width: '100%' }} size="small">
            <Space>
              <Tag color={CHECK_STATUS_COLOR[data.check.status] ?? 'default'}>{data.check.status}</Tag>
              <Text type="secondary">检查项 {data.check.findings.length} 条</Text>
            </Space>
            <Table<CheckFindingArtifact>
              rowKey="code"
              dataSource={data.check.findings}
              columns={checkColumns}
              pagination={false}
              locale={{ emptyText: '无检查发现' }}
              size="small"
            />
          </Space>
        ) : (
          <Text type="secondary">该审核无确定性检查记录。</Text>
        )}
      </Card>
      <Card title="审核动作">
        {data.review_action ? (
          <Descriptions size="small" column={2}>
            <Descriptions.Item label="动作类型">
              <Tag color={ACTION_TYPE_COLOR[data.review_action.action_type] ?? 'default'}>
                {ACTION_TYPE_LABEL[data.review_action.action_type] ?? data.review_action.action_type}
              </Tag>
            </Descriptions.Item>
            <Descriptions.Item label="问题数">{data.review_action.issue_count}</Descriptions.Item>
            <Descriptions.Item label="目标章节" span={2}>
              {data.review_action.target_section_ids.length > 0
                ? data.review_action.target_section_ids.join('、')
                : '—'}
            </Descriptions.Item>
          </Descriptions>
        ) : (
          <Text type="secondary">无审核动作记录。</Text>
        )}
      </Card>
      <Card title="人工审核">
        {data.human_review ? (
          <Descriptions size="small" column={2}>
            <Descriptions.Item label="决策">
              {data.human_review.decision ? (
                <Tag color={HUMAN_DECISION_COLOR[data.human_review.decision] ?? 'default'}>
                  {data.human_review.decision}
                </Tag>
              ) : (
                '待处理'
              )}
            </Descriptions.Item>
            <Descriptions.Item label="决策时间">{data.human_review.decided_at ?? '—'}</Descriptions.Item>
            <Descriptions.Item label="意见" span={2}>
              {data.human_review.comment_exists ? (data.human_review.comment ?? '—') : '—'}
            </Descriptions.Item>
          </Descriptions>
        ) : (
          <Text type="secondary">无人工审核请求。</Text>
        )}
      </Card>
      <Card title="补充研究">
        {data.research_backflow ? (
          <Descriptions size="small" column={2}>
            <Descriptions.Item label="状态">
              {data.research_backflow.fulfilled ? (
                <Tag color="green">已补充</Tag>
              ) : (
                <Tag color="orange">待补充</Tag>
              )}
            </Descriptions.Item>
            <Descriptions.Item label="请求 ID">
              <Text code>{data.research_backflow.research_request_id}</Text>
            </Descriptions.Item>
            <Descriptions.Item label="新综合结果 ID">
              {data.research_backflow.new_synthesis_result_id ? (
                <Text code>{data.research_backflow.new_synthesis_result_id}</Text>
              ) : (
                '—'
              )}
            </Descriptions.Item>
          </Descriptions>
        ) : (
          <Text type="secondary">无补充研究记录。</Text>
        )}
      </Card>
    </Space>
  );
}
