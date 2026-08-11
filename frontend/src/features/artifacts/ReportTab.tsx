/** 任务最新报告投影（verify_report_integrity read-side）。 */

import { useQuery } from '@tanstack/react-query';
import { Alert, Card, Descriptions, Typography } from 'antd';

import { getTaskReport, taskKeys } from '../../api/tasks';
import type { ReportArtifactResponse } from '../../types/artifacts';

const { Text } = Typography;

interface Props {
  taskId: string;
}

export function ReportTab({ taskId }: Props): React.JSX.Element {
  const { data, isLoading, isError } = useQuery({
    queryKey: taskKeys.report(taskId),
    queryFn: () => getTaskReport(taskId),
    refetchInterval: 5000,
  });

  if (isError) {
    return <Alert type="error" showIcon message="加载报告失败" />;
  }
  if (isLoading || !data) {
    return <Alert type="info" showIcon message="正在加载报告…" />;
  }
  if (!data.report_id) {
    return <Alert type="info" showIcon message="该任务尚无报告（未执行 Stage 5 或审核未通过）。" />;
  }
  return <ReportContent data={data} />;
}

function ReportContent({ data }: { data: ReportArtifactResponse }): React.JSX.Element {
  return (
    <Card title="报告概览">
      <Descriptions size="small" column={2}>
        <Descriptions.Item label="报告 ID">
          <Text code>{data.report_id}</Text>
        </Descriptions.Item>
        <Descriptions.Item label="大纲 ID">
          {data.outline_id ? <Text code>{data.outline_id}</Text> : '—'}
        </Descriptions.Item>
        <Descriptions.Item label="分析基准日">{data.analysis_as_of ?? '—'}</Descriptions.Item>
        <Descriptions.Item label="Schema 版本">{data.report_schema_version ?? '—'}</Descriptions.Item>
        <Descriptions.Item label="章节数">{data.section_count ?? '—'}</Descriptions.Item>
        <Descriptions.Item label="报告指纹">
          {data.report_fingerprint ? <Text code>{data.report_fingerprint}</Text> : '—'}
        </Descriptions.Item>
      </Descriptions>
    </Card>
  );
}
