/** 任务最新审核视图：audit 摘要 + issues。 */

import { useQuery } from '@tanstack/react-query';
import { Alert, Card, Descriptions, Space, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';

import { getTaskReviews, taskKeys } from '../../api/tasks';
import type { ReviewIssueArtifactResponse, ReviewsArtifactResponse } from '../../types/artifacts';

const { Text } = Typography;

const SEVERITY_COLOR: Record<string, string> = {
  critical: 'red',
  major: 'orange',
  minor: 'blue',
  info: 'default',
};

const ROUTE_LABEL: Record<string, string> = {
  approve: '批准',
  human_review: '人工审核',
  rewrite: '重写',
};

interface Props {
  taskId: string;
}

export function ReviewsTab({ taskId }: Props): React.JSX.Element {
  const { data, isLoading, isError } = useQuery({
    queryKey: taskKeys.reviews(taskId),
    queryFn: () => getTaskReviews(taskId),
    refetchInterval: 5000,
  });

  if (isError) {
    return <Alert type="error" showIcon message="加载审核视图失败" />;
  }
  if (isLoading || !data) {
    return <Alert type="info" showIcon message="正在加载审核视图…" />;
  }
  if (!data.audit_id) {
    return <Alert type="info" showIcon message="该任务尚无审核（未执行 Stage 5 审核）。" />;
  }
  return <ReviewsContent data={data} />;
}

function ReviewsContent({ data }: { data: ReviewsArtifactResponse }): React.JSX.Element {
  const columns: ColumnsType<ReviewIssueArtifactResponse> = [
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
  ];

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Card title="审核概览">
        <Descriptions size="small" column={2}>
          <Descriptions.Item label="审核 ID">
            <Text code>{data.audit_id}</Text>
          </Descriptions.Item>
          <Descriptions.Item label="报告 ID">
            {data.report_id ? <Text code>{data.report_id}</Text> : '—'}
          </Descriptions.Item>
          <Descriptions.Item label="审核状态">{data.audit_status ?? '—'}</Descriptions.Item>
          <Descriptions.Item label="建议路由">
            {data.recommended_route
              ? (ROUTE_LABEL[data.recommended_route] ?? data.recommended_route)
              : '—'}
          </Descriptions.Item>
          <Descriptions.Item label="问题数">{data.issue_count}</Descriptions.Item>
          <Descriptions.Item label="审核指纹">
            {data.audit_fingerprint ? <Text code>{data.audit_fingerprint}</Text> : '—'}
          </Descriptions.Item>
        </Descriptions>
      </Card>
      <Card title={`审核问题（${data.issue_count}）`}>
        <Table<ReviewIssueArtifactResponse>
          rowKey="review_issue_id"
          dataSource={data.issues}
          columns={columns}
          pagination={false}
          locale={{ emptyText: '无审核问题' }}
          size="small"
        />
      </Card>
    </Space>
  );
}
