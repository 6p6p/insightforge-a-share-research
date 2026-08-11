/** 公司级证据链产物计数卡片（spec K artifact summary）。 */

import { Card, Col, Row, Statistic } from 'antd';

import type { ArtifactSummary } from '../types/workspace';

const STAT_ITEMS: { key: keyof ArtifactSummary; label: string }[] = [
  { key: 'source_count', label: '来源' },
  { key: 'evidence_count', label: '证据' },
  { key: 'claim_count', label: '观点' },
  { key: 'report_count', label: '报告' },
  { key: 'review_issue_count', label: '审核问题' },
];

interface Props {
  summary: ArtifactSummary;
}

export function ArtifactSummaryCards({ summary }: Props): React.JSX.Element {
  return (
    <Row gutter={16}>
      {STAT_ITEMS.map((item) => (
        <Col key={item.key} xs={12} md={4} lg={4}>
          <Card size="small">
            <Statistic title={item.label} value={summary[item.key]} />
          </Card>
        </Col>
      ))}
    </Row>
  );
}
