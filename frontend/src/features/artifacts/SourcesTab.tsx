/** 任务引用的 source 列表（任务级 scoped，分页，stage 6B.1 dual-origin）。

document_chunk（source_id 非空）与 macro_observation（source_id=NULL，
source_identity 由 provider/series/snapshot 恢复）都按任务级精确集合展示。
 */

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Alert, Table, Tag } from 'antd';
import type { ColumnsType } from 'antd/es/table';

import { getTaskSources, taskKeys } from '../../api/tasks';
import type { SourceArtifactResponse } from '../../types/artifacts';
import {
  PROVIDER_LABEL,
  SOURCE_TYPE_LABEL,
  displayLabel,
} from '../../utils/display';
import { artifactErrorMessage } from './integrity';

const STATUS_COLOR: Record<string, string> = {
  available: 'green',
  pending: 'orange',
  failed: 'red',
  disabled: 'default',
};

/** 来源状态 → 产品语义（V1.1 不暴露内部枚举）。 */
const STATUS_LABEL: Record<string, string> = {
  available: '可用',
  pending: '处理中',
  failed: '失败',
  disabled: '已停用',
};

const ORIGIN_LABEL: Record<string, { label: string; color: string }> = {
  document_chunk: { label: '文档', color: 'blue' },
  macro_observation: { label: '宏观序列', color: 'cyan' },
};

interface Props {
  taskId: string;
}

export function SourcesTab({ taskId }: Props): React.JSX.Element {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const { data, isLoading, isError, error } = useQuery({
    queryKey: taskKeys.sources(taskId, { limit: pageSize, offset: (page - 1) * pageSize }),
    queryFn: () => getTaskSources(taskId, { limit: pageSize, offset: (page - 1) * pageSize }),
    refetchInterval: 5000,
  });

  const columns: ColumnsType<SourceArtifactResponse> = [
    {
      title: '来源类型',
      dataIndex: 'origin_type',
      width: 110,
      render: (v: string) => {
        const cfg = ORIGIN_LABEL[v] ?? { label: v, color: 'default' };
        return <Tag color={cfg.color}>{cfg.label}</Tag>;
      },
    },
    { title: '标题', dataIndex: 'title', ellipsis: true, render: (v: string | null) => v ?? '—' },
    {
      title: '类型',
      width: 140,
      render: (_, row) =>
        displayLabel(SOURCE_TYPE_LABEL, row.source_type ?? row.document_type),
    },
    {
      title: '提供方',
      dataIndex: 'provider_key',
      width: 110,
      render: (v: string | null) => displayLabel(PROVIDER_LABEL, v),
    },
    { title: '标签', dataIndex: 'label', width: 120, render: (v: string | null) => v ?? '—' },
    {
      title: '权威级别',
      dataIndex: 'authority_tier',
      width: 100,
      render: (v: number | null) => v ?? '—',
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 100,
      render: (v: string | null) =>
        v ? (
          <Tag color={STATUS_COLOR[v] ?? 'default'}>{STATUS_LABEL[v] ?? v}</Tag>
        ) : (
          '—'
        ),
    },
    {
      title: '定位摘要',
      dataIndex: 'locator_summary',
      ellipsis: true,
      render: (v: string | null) => v ?? '—',
    },
  ];

  if (isError) {
    return <Alert type="error" showIcon message={artifactErrorMessage(error, '加载来源列表失败')} />;
  }

  return (
    <Table<SourceArtifactResponse>
      rowKey={(row) => row.source_id ?? row.source_identity}
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
