/** 评估 Benchmark 对比页（§25 最小只读视图）。

从 CLI workspace（results.json）读取三路 variant 的 attempt 记录并对比展示。
只读：不触发执行 / 不写库。无 ECharts 依赖（不新增框架），用 Ant Design
Table + Statistic 呈现。
 */

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Alert, Card, Col, Layout, Row, Segmented, Statistic, Table, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';

import {
  evalBenchmarkKeys,
  getEvalBenchmarkSummary,
  type BenchmarkAttemptRecord,
  type BenchmarkRunMode,
} from '../api/evalBenchmark';
import { PageTitle } from '../components/PageTitle';
import { StatusTag } from '../components/StatusTag';

function metricText(metric: BenchmarkAttemptRecord['citation_validity']): string {
  if (!metric) {
    return '—';
  }
  if (metric.status === 'computed' && metric.value !== null) {
    return `${metric.value}（${metric.numerator}/${metric.denominator}）`;
  }
  return metric.status;
}

export function EvalBenchmarkPage(): React.JSX.Element {
  const [run, setRun] = useState<BenchmarkRunMode>('fake');
  const { data, isLoading, isError, error } = useQuery({
    queryKey: evalBenchmarkKeys.summary(run),
    queryFn: () => getEvalBenchmarkSummary(run),
    retry: false,
  });

  const attempts = data?.attempts ?? [];
  const successCount = attempts.filter((a) => a.status === 'success').length;
  const totalCost = attempts.reduce((sum, a) => {
    const cost = a.estimated_cost_usd === null ? null : Number(a.estimated_cost_usd);
    return cost === null || Number.isNaN(cost) ? sum : sum + cost;
  }, 0);
  const totalCalls = attempts.reduce((sum, a) => sum + a.usage_call_count, 0);

  const columns: ColumnsType<BenchmarkAttemptRecord> = [
    { title: 'Case', dataIndex: 'case_id', width: 160 },
    { title: 'Variant', dataIndex: 'variant_id', width: 190 },
    {
      title: '状态',
      dataIndex: 'status',
      width: 110,
      render: (status: BenchmarkAttemptRecord['status'], record) =>
        record.error_code ? (
          <Typography.Text type="danger" title={record.error_code}>
            {status}（{record.error_code}）
          </Typography.Text>
        ) : (
          <StatusTag kind="task" status={status === 'success' ? 'completed' : 'failed'} />
        ),
    },
    {
      title: '延迟 (ms)',
      dataIndex: 'wall_latency_ms',
      width: 100,
      render: (v: number | null) => (v === null ? '—' : String(v)),
    },
    { title: 'LLM 调用', dataIndex: 'usage_call_count', width: 90 },
    {
      title: 'Tokens',
      dataIndex: 'total_tokens',
      width: 100,
      render: (v: number | null) => (v === null ? '—' : String(v)),
    },
    {
      title: '成本 (USD)',
      dataIndex: 'estimated_cost_usd',
      width: 110,
      render: (v: string | null) => (v === null ? '—' : v),
    },
    {
      title: 'Citation Validity',
      dataIndex: 'citation_validity',
      width: 130,
      render: (m: BenchmarkAttemptRecord['citation_validity']) => metricText(m),
    },
    {
      title: 'Citation Coverage',
      dataIndex: 'citation_coverage',
      width: 130,
      render: (m: BenchmarkAttemptRecord['citation_coverage']) => metricText(m),
    },
    {
      title: 'Output Fingerprint',
      dataIndex: 'variant_output_fingerprint',
      render: (fp: string | null) =>
        fp ? <Typography.Text code>{fp.slice(0, 12)}…</Typography.Text> : '—',
    },
  ];

  return (
    <Layout.Content style={{ padding: 24 }}>
      <PageTitle title="评估 Benchmark 对比" />
      <Card>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
          <Segmented<BenchmarkRunMode>
            options={[
              { label: '离线 (fake)', value: 'fake' },
              { label: '真实模型 (real)', value: 'real' },
            ]}
            value={run}
            onChange={(value) => setRun(value)}
          />
          {data && (
            <Typography.Text type="secondary">
              dataset {data.dataset_id} v{data.dataset_version} · as_of {data.as_of} ·{' '}
              {data.model}
            </Typography.Text>
          )}
        </div>
        {isError && (
          <Alert
            type="warning"
            showIcon
            message="无法读取 benchmark 结果"
            description={
              error instanceof Error
                ? `${error.message}（先执行 python -m app.eval.cli run --${run} 生成）`
                : '未知错误'
            }
            style={{ marginBottom: 16 }}
          />
        )}
        <Row gutter={16} style={{ marginBottom: 16 }}>
          <Col span={6}>
            <Statistic title="Attempt 数" value={attempts.length} />
          </Col>
          <Col span={6}>
            <Statistic title="成功" value={successCount} />
          </Col>
          <Col span={6}>
            <Statistic title="LLM 调用总数" value={totalCalls} />
          </Col>
          <Col span={6}>
            <Statistic title="估算总成本 (USD)" value={totalCost} precision={6} />
          </Col>
        </Row>
        <Table<BenchmarkAttemptRecord>
          rowKey={(record) => `${record.case_id}:${record.variant_id}:${record.attempt_no}`}
          loading={isLoading}
          dataSource={attempts}
          columns={columns}
          pagination={false}
          size="small"
          locale={{ emptyText: <Typography.Text type="secondary">暂无 benchmark 结果</Typography.Text> }}
        />
      </Card>
    </Layout.Content>
  );
}
