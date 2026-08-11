/** 任务引用的 evidence card 列表（任务级 scoped，分页，stage 6B.1）。

展示 used_by_claim_ids / claim_relations（canonical synthesis 引用关系）与
macro 卡专用标识（macro_observation_id / snapshot_id / series_id）。
 */

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Alert, Space, Table, Tag, Tooltip, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';

import { getTaskEvidence, taskKeys } from '../../api/tasks';
import type { EvidenceArtifactResponse } from '../../types/artifacts';
import { artifactErrorMessage } from './integrity';

const { Text } = Typography;

const TYPE_COLOR: Record<string, string> = {
  financial: 'blue',
  metric: 'purple',
  macro: 'cyan',
  document_chunk: 'geekblue',
  regulation: 'volcano',
};

const RELATION_COLOR: Record<string, string> = {
  supports: 'green',
  contradicts: 'red',
  context: 'default',
};

interface Props {
  taskId: string;
}

export function EvidenceTab({ taskId }: Props): React.JSX.Element {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const { data, isLoading, isError, error } = useQuery({
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
    {
      title: '引用观点',
      width: 130,
      render: (_, row) => {
        if (row.claim_relations.length === 0) {
          return <Text type="secondary">—</Text>;
        }
        const relationTags = row.claim_relations.map((rel) => (
          <Tag key={`${rel.claim_id}:${rel.relation}`} color={RELATION_COLOR[rel.relation] ?? 'default'}>
            {rel.relation}·{rel.claim_id.slice(0, 8)}
          </Tag>
        ));
        return (
          <Tooltip title={relationTags}>
            <Text>{row.used_by_claim_ids.length} 个观点</Text>
          </Tooltip>
        );
      },
    },
    { title: '提取置信度', dataIndex: 'extractor_confidence', width: 120 },
    { title: '来源类型', dataIndex: 'origin_type', width: 120 },
    {
      title: '宏观标识',
      width: 200,
      render: (_, row) =>
        row.macro_series_id ? (
          <Space size={4}>
            <Tag color="cyan">系列 {row.macro_series_id.slice(0, 8)}</Tag>
            {row.macro_observation_id ? (
              <Text type="secondary" style={{ fontSize: 12 }}>
                {row.macro_observation_id.slice(0, 8)}
              </Text>
            ) : null}
          </Space>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      width: 180,
      render: (v: string) => v,
    },
  ];

  if (isError) {
    return <Alert type="error" showIcon message={artifactErrorMessage(error, '加载证据列表失败')} />;
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
