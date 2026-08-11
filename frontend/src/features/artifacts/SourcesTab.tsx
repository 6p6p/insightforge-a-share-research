/** 任务引用的 source 列表（任务级 scoped，分页）。 */

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Alert, Table, Tag } from 'antd';
import type { ColumnsType } from 'antd/es/table';

import { getTaskSources, taskKeys } from '../../api/tasks';
import type { SourceArtifactResponse } from '../../types/artifacts';

const STATUS_COLOR: Record<string, string> = {
  available: 'green',
  pending: 'orange',
  failed: 'red',
  disabled: 'default',
};

interface Props {
  taskId: string;
}

export function SourcesTab({ taskId }: Props): React.JSX.Element {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const { data, isLoading, isError } = useQuery({
    queryKey: taskKeys.sources(taskId, { limit: pageSize, offset: (page - 1) * pageSize }),
    queryFn: () => getTaskSources(taskId, { limit: pageSize, offset: (page - 1) * pageSize }),
    refetchInterval: 5000,
  });

  const columns: ColumnsType<SourceArtifactResponse> = [
    { title: '标题', dataIndex: 'title', ellipsis: true },
    { title: '来源', dataIndex: 'provider_key', width: 120 },
    { title: '类型', dataIndex: 'document_type', width: 140 },
    {
      title: '状态',
      dataIndex: 'status',
      width: 100,
      render: (v: string) => <Tag color={STATUS_COLOR[v] ?? 'default'}>{v}</Tag>,
    },
    {
      title: '发布时间',
      dataIndex: 'published_at',
      width: 180,
      render: (v: string | null) => v ?? '—',
    },
  ];

  if (isError) {
    return <Alert type="error" showIcon message="加载来源列表失败" />;
  }

  return (
    <Table<SourceArtifactResponse>
      rowKey="source_id"
      dataSource={data?.items}
      loading={isLoading}
      columns={columns}
      locale={{ emptyText: '该任务尚无引用来源' }}
      pagination={{
        current: page,
        pageSize,
        total: data?.total ?? 0,
        showSizeChanger: true,
        pageSizeOptions: [10, 20, 50, 100],
        onChange: (p, ps) => {
          setPage(p);
          setPageSize(ps);
        },
      }}
    />
  );
}
