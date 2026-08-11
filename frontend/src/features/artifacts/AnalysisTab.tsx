/** 任务 Stage 4 分析视图：work items + claims + synthesis 摘要。 */

import { useQuery } from '@tanstack/react-query';
import { Alert, Card, Descriptions, List, Space, Tag, Typography } from 'antd';

import { getTaskAnalysis, taskKeys } from '../../api/tasks';
import type { AnalysisArtifactResponse, ClaimArtifactResponse, WorkItemSummary } from '../../types/artifacts';

const { Text } = Typography;

/** 汇总一个 work item 的输入证据 / 计算 / 对比 ID 数量。 */
function inputCounts(item: WorkItemSummary): string[] {
  const parts: string[] = [];
  if (item.evidence_card_ids.length) parts.push(`证据 ${item.evidence_card_ids.length}`);
  if (item.additional_evidence_ids.length) parts.push(`附加 ${item.additional_evidence_ids.length}`);
  if (item.macro_driver_evidence_ids.length) parts.push(`宏观 ${item.macro_driver_evidence_ids.length}`);
  if (item.company_evidence_ids.length) parts.push(`公司证据 ${item.company_evidence_ids.length}`);
  if (item.calculation_ids.length) parts.push(`计算 ${item.calculation_ids.length}`);
  if (item.comparison_ids.length) parts.push(`对比 ${item.comparison_ids.length}`);
  return parts;
}

const DOMAIN_COLOR: Record<string, string> = {
  business: 'blue',
  risk: 'red',
  financial: 'green',
  macro: 'cyan',
  valuation: 'purple',
};

interface Props {
  taskId: string;
}

export function AnalysisTab({ taskId }: Props): React.JSX.Element {
  const { data, isLoading, isError } = useQuery({
    queryKey: taskKeys.analysis(taskId),
    queryFn: () => getTaskAnalysis(taskId),
    refetchInterval: 5000,
  });

  if (isError) {
    return <Alert type="error" showIcon message="加载分析视图失败" />;
  }
  if (isLoading || !data) {
    return <Alert type="info" showIcon message="正在加载分析视图…" />;
  }
  return <AnalysisContent data={data} />;
}

function AnalysisContent({ data }: { data: AnalysisArtifactResponse }): React.JSX.Element {
  const hasArtifacts = data.work_items.length > 0 || data.claims.length > 0;
  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Card title="分析概况">
        <Descriptions size="small" column={2}>
          <Descriptions.Item label="研究问题">{data.research_question ?? '—'}</Descriptions.Item>
          <Descriptions.Item label="分析基准日">{data.analysis_as_of ?? '—'}</Descriptions.Item>
          <Descriptions.Item label="合成运行 ID">{data.synthesis_id ?? '—'}</Descriptions.Item>
          <Descriptions.Item label="合成指纹">
            {data.synthesis_fingerprint ? <Text code>{data.synthesis_fingerprint}</Text> : '—'}
          </Descriptions.Item>
        </Descriptions>
      </Card>
      {!hasArtifacts ? (
        <Alert type="info" showIcon message="该任务尚无分析产物（未执行 Stage 4 或执行未完成）。" />
      ) : null}
      {data.work_items.length > 0 ? (
        <Card title={`分析工作项（${data.work_items.length}）`}>
          <List
            dataSource={data.work_items}
            renderItem={(item) => (
              <List.Item>
                <Space direction="vertical" size={4}>
                  <Space>
                    <Tag color={DOMAIN_COLOR[item.analysis_type] ?? 'default'}>{item.analysis_type}</Tag>
                    <Text type="secondary">item: {item.item_id}</Text>
                    <Text type="secondary">产物 claim: {item.claim_ids.length}</Text>
                  </Space>
                  <Text type="secondary">
                    输入：{inputCounts(item).join(' / ') || '无'}
                  </Text>
                </Space>
              </List.Item>
            )}
          />
        </Card>
      ) : null}
      {data.claims.length > 0 ? (
        <Card title={`观点（${data.claims.length}）`}>
          <List
            dataSource={data.claims}
            renderItem={(claim) => <ClaimRow claim={claim} />}
          />
        </Card>
      ) : null}
    </Space>
  );
}

function ClaimRow({ claim }: { claim: ClaimArtifactResponse }): React.JSX.Element {
  return (
    <List.Item>
      <Space direction="vertical" size={4} style={{ width: '100%' }}>
        <Space wrap>
          <Tag color={DOMAIN_COLOR[claim.analysis_domain] ?? 'default'}>{claim.analysis_domain}</Tag>
          <Tag>{claim.claim_kind}</Tag>
          <Tag color={claim.confidence === 'high' ? 'green' : claim.confidence === 'medium' ? 'orange' : 'default'}>
            置信 {claim.confidence}
          </Tag>
          <Tag color={claim.importance === 'high' ? 'volcano' : 'default'}>重要 {claim.importance}</Tag>
          <Text type="secondary">证据 {claim.evidence_card_ids.length} 条</Text>
        </Space>
        <Text>{claim.statement}</Text>
      </Space>
    </List.Item>
  );
}
