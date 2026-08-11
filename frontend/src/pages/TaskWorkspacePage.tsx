/** 任务工作台页（spec K + Stage 6B.1 artifact workspace）。

SSE 事件到达时自动失效 workspace 缓存 → TanStack Query 后台重取；
pending human action 出现时渲染 HumanActionCard。

Stage 6B.1：页面用 antd Tabs 组织——「概览」tab 保留原任务概要 + 产物计数 +
进度 + SSE 时间线；新增 5 个**任务级**只读 artifact tab（来源 / 证据 / 分析 /
报告 / 审核）。antd v5 Tabs 惰性挂载 → 各 tab 的 useQuery 首次激活才触发。
 */

import { useState } from 'react';
import { useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Alert, Badge, Button, Card, Descriptions, Layout, Space, Tabs, Typography } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';

import { getTaskWorkspace, taskKeys } from '../api/tasks';
import { ArtifactSummaryCards } from '../components/ArtifactSummaryCards';
import { PageTitle } from '../components/PageTitle';
import { StatusTag } from '../components/StatusTag';
import { useTaskEvents } from '../hooks/useTaskEvents';
import { StartResearchPanel } from '../features/workflow-progress/StartResearchPanel';
import { WorkflowProgressPanel } from '../features/workflow-progress/WorkflowProgressPanel';
import { AnalysisTab } from '../features/artifacts/AnalysisTab';
import { EvidenceTab } from '../features/artifacts/EvidenceTab';
import { ReportTab } from '../features/artifacts/ReportTab';
import { ReviewsTab } from '../features/artifacts/ReviewsTab';
import { SourcesTab } from '../features/artifacts/SourcesTab';
import { ApiError } from '../types/api';
import type { WorkflowEventResponse, WorkflowRunResponse } from '../types/workflow';
import type { TaskWorkspaceResponse } from '../types/workspace';

type UseQueryResult = {
  data: TaskWorkspaceResponse | undefined;
  isLoading: boolean;
  isError: boolean;
  error: unknown;
  refetch: () => Promise<unknown>;
};

export function TaskWorkspacePage(): React.JSX.Element {
  const { taskId = '' } = useParams<{ taskId: string }>();
  const [activeTab, setActiveTab] = useState('overview');

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: taskKeys.workspace(taskId),
    queryFn: () => getTaskWorkspace(taskId),
    refetchInterval: 5000,
  });

  const { events, connected, streamEnded, error: sseError } = useTaskEvents(taskId);

  const run = data?.current_run ?? null;
  const hasActiveRun = run != null && ['pending', 'running', 'waiting_human'].includes(run.status);

  return (
    <Layout.Content style={{ padding: 24 }}>
      <PageTitle
        title="任务工作台"
        subTitle={data ? `${data.task.company_query}` : taskId}
        extra={
          <Space>
            <Badge
              status={streamEnded ? 'success' : connected ? 'processing' : 'default'}
              text={streamEnded ? '事件流已结束' : connected ? '实时连接' : '连接中…'}
            />
            <Button icon={<ReloadOutlined />} onClick={() => void refetch()}>
              刷新
            </Button>
          </Space>
        }
      />

      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={[
          {
            key: 'overview',
            label: '概览',
            children: (
              <OverviewPanel
                taskId={taskId}
                query={{ data, isLoading, isError, error, refetch }}
                run={run}
                hasActiveRun={hasActiveRun}
                events={events}
                sseError={sseError}
              />
            ),
          },
          { key: 'sources', label: '来源', children: <SourcesTab taskId={taskId} /> },
          { key: 'evidence', label: '证据', children: <EvidenceTab taskId={taskId} /> },
          { key: 'analysis', label: '分析', children: <AnalysisTab taskId={taskId} /> },
          { key: 'report', label: '报告', children: <ReportTab taskId={taskId} /> },
          { key: 'reviews', label: '审核', children: <ReviewsTab taskId={taskId} /> },
        ]}
      />
    </Layout.Content>
  );
}

interface OverviewPanelProps {
  taskId: string;
  query: UseQueryResult;
  run: WorkflowRunResponse | null;
  hasActiveRun: boolean;
  events: WorkflowEventResponse[];
  sseError: string | null;
}

/** 「概览」tab：原任务工作台内容（spec K），保持原行为。 */
function OverviewPanel({
  taskId,
  query,
  run,
  hasActiveRun,
  events,
  sseError,
}: OverviewPanelProps): React.JSX.Element | null {
  const { data, isLoading, isError, error, refetch } = query;

  if (isLoading) {
    return <Card loading />;
  }

  if (isError) {
    return (
      <Alert
        type="error"
        showIcon
        message="加载任务工作台失败"
        description={error instanceof ApiError ? error.message : String(error)}
      />
    );
  }

  if (!data) {
    return null;
  }

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Card title="任务概要">
        <Descriptions size="small" column={3}>
          <Descriptions.Item label="状态">
            <StatusTag kind="task" status={data.task.status} />
          </Descriptions.Item>
          <Descriptions.Item label="公司">{data.task.company_query}</Descriptions.Item>
          <Descriptions.Item label="分析周期">
            {data.task.research_start_date} ~ {data.task.research_end_date}
          </Descriptions.Item>
          <Descriptions.Item label="模块">{data.task.modules.join('、')}</Descriptions.Item>
          <Descriptions.Item label="研究问题">
            {data.task.questions.length > 0 ? data.task.questions[0] : '—'}
          </Descriptions.Item>
          <Descriptions.Item label="需要计划审批">
            {data.task.require_plan_approval ? '是' : '否'}
          </Descriptions.Item>
        </Descriptions>
        {data.resolved_company ? (
          <Descriptions size="small" column={3} style={{ marginTop: 8 }}>
            <Descriptions.Item label="公司名称">{data.resolved_company.company_name}</Descriptions.Item>
            <Descriptions.Item label="证券代码">{data.resolved_company.security_code ?? '—'}</Descriptions.Item>
            <Descriptions.Item label="行业">{data.resolved_company.industry ?? '—'}</Descriptions.Item>
          </Descriptions>
        ) : null}
      </Card>

      <Card title="证据链产物">
        <ArtifactSummaryCards summary={data.artifact_summary} />
      </Card>

      {run ? (
        <WorkflowProgressPanel task={data.task} run={run} events={events} />
      ) : null}

      {!hasActiveRun ? <StartResearchPanel taskId={taskId} onStarted={() => void refetch()} /> : null}

      {sseError ? <Alert type="warning" showIcon message={sseError} /> : null}

      {data.task.status === 'failed' ? (
        <Typography.Text type="danger">任务已失败，可重新启动研究执行。</Typography.Text>
      ) : null}
    </Space>
  );
}
