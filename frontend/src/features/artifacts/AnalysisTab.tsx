/** 任务 Stage 4 分析视图：work items + claims + synthesis 摘要 + 结构化综合。

stage 6B.1 spec B/H：`synthesis_result_id` 是 canonical synthesis；`work_items`
只在匹配到同一 synthesis 的 Stage4 run 时暴露（`work_items_available=false`
时显示提示，绝不混用旧工作项）；themes / conflicts / evidence_gaps 按真实
SynthesisResult v1 contract 投影（alias refs 已解析为真实 claim_ids）。
 */

import { useQuery } from '@tanstack/react-query';
import { Alert, Card, Descriptions, List, Space, Tag, Typography } from 'antd';

import { getTaskAnalysis, taskKeys } from '../../api/tasks';
import type {
  AnalysisArtifactResponse,
  ClaimArtifactResponse,
  SynthesisEvidenceGapArtifact,
  WorkItemSummary,
} from '../../types/artifacts';
import { artifactErrorMessage } from './integrity';

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

const CONFLICT_SEVERITY_COLOR: Record<string, string> = {
  critical: 'red',
  major: 'orange',
  minor: 'blue',
  info: 'default',
};

const GAP_PRIORITY_COLOR: Record<string, string> = {
  high: 'volcano',
  medium: 'orange',
  low: 'default',
};

interface Props {
  taskId: string;
}

export function AnalysisTab({ taskId }: Props): React.JSX.Element {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: taskKeys.analysis(taskId),
    queryFn: () => getTaskAnalysis(taskId),
    refetchInterval: 5000,
  });

  if (isError) {
    return <Alert type="error" showIcon message={artifactErrorMessage(error, '加载分析视图失败')} />;
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
          <Descriptions.Item label="综合运行 ID">
            <Text code>{data.synthesis_id ?? '—'}</Text>
          </Descriptions.Item>
          <Descriptions.Item label="综合结果 ID">
            {data.synthesis_result_id ? <Text code>{data.synthesis_result_id}</Text> : '—'}
          </Descriptions.Item>
          <Descriptions.Item label="综合指纹">
            {data.synthesis_fingerprint ? <Text code>{data.synthesis_fingerprint}</Text> : '—'}
          </Descriptions.Item>
          <Descriptions.Item label="结果指纹">
            {data.result_fingerprint ? <Text code>{data.result_fingerprint}</Text> : '—'}
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
      {data.themes.length > 0 ? (
        <Card title={`综合主题（${data.themes.length}）`}>
          <List
            dataSource={data.themes}
            renderItem={(theme) => (
              <List.Item>
                <Space direction="vertical" size={4} style={{ width: '100%' }}>
                  <Space>
                    <Text strong>{theme.title}</Text>
                    <Text type="secondary">关联观点 {theme.claim_ids.length}</Text>
                  </Space>
                  <Text>{theme.summary}</Text>
                </Space>
              </List.Item>
            )}
          />
        </Card>
      ) : null}
      {data.conflicts.length > 0 ? (
        <Card title={`观点冲突（${data.conflicts.length}）`}>
          <List
            dataSource={data.conflicts}
            renderItem={(conflict) => (
              <List.Item>
                <Space direction="vertical" size={4} style={{ width: '100%' }}>
                  <Space>
                    <Tag color={CONFLICT_SEVERITY_COLOR[conflict.severity] ?? 'default'}>
                      {conflict.severity}
                    </Tag>
                    <Text type="secondary">关联观点 {conflict.claim_ids.length}</Text>
                  </Space>
                  <Text>{conflict.description}</Text>
                  <Text type="secondary">解决方向：{conflict.resolution_direction}</Text>
                </Space>
              </List.Item>
            )}
          />
        </Card>
      ) : null}
      {data.evidence_gaps.length > 0 ? (
        <Card title={`证据缺口（${data.evidence_gaps.length}）`}>
          <List
            dataSource={data.evidence_gaps}
            renderItem={(gap) => <EvidenceGapRow gap={gap} />}
          />
        </Card>
      ) : null}
      {data.work_items.length === 0 && !data.work_items_available ? (
        <Alert
          type="warning"
          showIcon
          message="未匹配到可用的分析工作项"
          description="当前综合结果未匹配到同一 Synthesis 的 Stage 4 工作项（例如经由研究回流的全新综合）。为保持证据链一致性，不展示旧任务的工作项。"
        />
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

function EvidenceGapRow({ gap }: { gap: SynthesisEvidenceGapArtifact }): React.JSX.Element {
  return (
    <List.Item>
      <Space direction="vertical" size={4} style={{ width: '100%' }}>
        <Space>
          <Tag color={GAP_PRIORITY_COLOR[gap.priority] ?? 'default'}>优先级 {gap.priority}</Tag>
          <Text type="secondary">关联观点 {gap.claim_ids.length}</Text>
        </Space>
        <Text>{gap.description}</Text>
        {gap.suggested_evidence ? (
          <Text type="secondary">建议补充：{gap.suggested_evidence}</Text>
        ) : null}
      </Space>
    </List.Item>
  );
}
