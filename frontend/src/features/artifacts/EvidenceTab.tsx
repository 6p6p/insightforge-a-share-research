/** 任务引用的 evidence card 列表（任务级 scoped，分页）。 */

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Alert, Table, Tag } from 'antd';
import type { ColumnsType } from 'antd/es/table';

import { getTaskEvidence, taskKeys } from '../../api/tasks';
import type { EvidenceArtifactResponse } from '../../types/artifacts';

const TYPE_COLOR: Record<string, string> = {
  financial: 'blue',
  metric: 'purple',
  macro: 'cyan',
  document_chunk: 'geekblue',
  regulation: 'volcano',
};

interface Props {
  taskId: string;
}

export function EvidenceTab({ taskId }: Props): React.JSX.Element {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const { data, isLoading, isError } = useQuery({
    queryKey: taskKeys.evidence(taskId, { limit: pageSize, offset: (page - 1) * pageSize }),
    queryFn: () => getTaskEvidence(taskId, { limit: pageSize, offset: (page - 1) * pageSize }),
    refetchInterval: 5000,
  });

  const columns: ColumnsType<EvidenceArtifactResponse> = [
    { title: '证据陈述', dataIndex: 'evidence_statement', ellipsis: true },
    {
      title: '类型',
      dataIndex: 'evidence_type',
      width: 140,
      render: (v: string) => <Tag color={TYPE_COLOR[v] ?? 'default'}>{v}</Tag>,
    },
    { title: '提取置信度', dataIndex: 'extractor_confidence', width: 120 },
    { title: '来源类型', dataIndex: 'origin_type', width: 120 },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      width: 180,
      render: (v: string) => v,
    },
  ];

  if (isError) {
    return <Alert type="error" showIcon message="加载证据列表失败" />;
  }

  return (
    <Table<EvidenceArtifactResponse>
      rowKey="evidence_card_id"
      dataSource={data?.items}
      loading={isLoading}
      columns={columns}
      locale={{ emptyText: '该任务尚无引用证据' }}
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
