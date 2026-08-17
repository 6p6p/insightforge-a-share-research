/** 任务列表页：简洁表格，点击跳转任务工作台。 */

import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Card, Layout, Table, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';

import { listTasks, taskKeys } from '../api/tasks';
import { PageTitle } from '../components/PageTitle';
import { StatusTag } from '../components/StatusTag';
import type { TaskResponse } from '../types/task';
import { PUBLIC_STATUS_LABEL, stageLabel } from '../utils/status';

export function TaskListPage(): React.JSX.Element {
  const { data, isLoading } = useQuery({
    queryKey: taskKeys.list({ limit: 20, offset: 0 }),
    queryFn: () => listTasks({ limit: 20, offset: 0 }),
  });

  const columns: ColumnsType<TaskResponse> = [
    {
      title: '公司',
      dataIndex: 'company_query',
      render: (value: string, record) => <Link to={`/tasks/${record.task_id}`}>{value}</Link>,
    },
    {
      title: '状态',
      dataIndex: 'public_status',
      render: (status: TaskResponse['public_status']) => (
        <StatusTag kind="public" status={status} />
      ),
    },
    {
      title: '阶段',
      dataIndex: 'current_stage',
      render: (stage: TaskResponse['current_stage']) => stageLabel(stage),
    },
    {
      title: '进度',
      dataIndex: 'public_status',
      render: (status: TaskResponse['public_status']) => PUBLIC_STATUS_LABEL[status] ?? status,
    },
    { title: '分析周期', dataIndex: 'research_start_date', render: (_, record) => `${record.research_start_date} ~ ${record.research_end_date}` },
    { title: '创建时间', dataIndex: 'created_at', render: (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false }) },
  ];

  return (
    <Layout.Content style={{ padding: 24 }}>
      <PageTitle title="研究任务" />
      <Card>
        <Table<TaskResponse>
          rowKey="task_id"
          loading={isLoading}
          dataSource={data?.items ?? []}
          columns={columns}
          pagination={false}
          locale={{ emptyText: <Typography.Text type="secondary">暂无研究任务</Typography.Text> }}
        />
      </Card>
    </Layout.Content>
  );
}
