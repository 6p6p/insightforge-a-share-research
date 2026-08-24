/** 任务列表页：简洁表格，点击跳转任务工作台；支持删除任务（硬删除，带确认弹窗）。 */

import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button, Card, Layout, message, Popconfirm, Table, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';

import { archiveTask, listTasks, taskKeys } from '../api/tasks';
import { PageTitle } from '../components/PageTitle';
import { StatusTag } from '../components/StatusTag';
import type { TaskResponse } from '../types/task';
import { PUBLIC_STATUS_LABEL, publicStatusText, stageLabel } from '../utils/status';

export function TaskListPage(): React.JSX.Element {
  const queryClient = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: taskKeys.list({ limit: 20, offset: 0 }),
    queryFn: () => listTasks({ limit: 20, offset: 0 }),
  });

  const archiveMutation = useMutation({
    mutationFn: (taskId: string) => archiveTask(taskId),
    onSuccess: () => {
      message.success('任务已归档');
      void queryClient.invalidateQueries({ queryKey: taskKeys.all });
    },
    onError: (error: unknown) => {
      message.error(
        error instanceof Error ? error.message : '归档研究任务失败，请稍后重试',
      );
    },
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
      render: (status: TaskResponse['public_status'], record) => (
        <StatusTag
          kind="public"
          status={status}
          completedWithWarnings={record.completed_with_warnings}
        />
      ),
    },
    {
      title: '阶段',
      dataIndex: 'current_stage',
      render: (stage: TaskResponse['current_stage'], record) =>
        record.public_status === 'completed' || record.public_status === 'failed'
          ? PUBLIC_STATUS_LABEL[record.public_status]
          : stageLabel(stage),
    },
    {
      title: '进度',
      dataIndex: 'public_status',
      render: (status: TaskResponse['public_status'], record) =>
        publicStatusText(status, record.completed_with_warnings),
    },
    { title: '分析周期', dataIndex: 'research_start_date', render: (_, record) => `${record.research_start_date} ~ ${record.research_end_date}` },
    { title: '创建时间', dataIndex: 'created_at', render: (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false }) },
    {
      title: '操作',
      key: 'actions',
      width: 90,
      render: (_, record) => (
        <Popconfirm
          title="归档研究任务"
          description="确认归档该研究任务？归档后任务将从任务列表隐藏，但研究数据会保留。"
          okText="确认归档"
          cancelText="取消"
          okButtonProps={{ loading: archiveMutation.isPending }}
          onConfirm={() => archiveMutation.mutate(record.task_id)}
        >
          <Button type="link" size="small">
            归档
          </Button>
        </Popconfirm>
      ),
    },
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
