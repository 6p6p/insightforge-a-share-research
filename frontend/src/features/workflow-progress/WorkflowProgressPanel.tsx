/** 工作流进度面板（spec K）。

任务概要 + 当前 run 状态 + 真实工作流阶段 + progress + node/event timeline
+ pending human action + error 信息。简单 Timeline/Steps，不做 DAG 编辑器。
 */

import { Alert, Card, Descriptions, Empty, Progress, Space, Steps, Typography } from 'antd';

import { EventTimeline } from '../../components/EventTimeline';
import { StatusTag } from '../../components/StatusTag';
import type { TaskResponse } from '../../types/task';
import type { WorkflowEventResponse, WorkflowRunResponse } from '../../types/workflow';
import { stageLabel } from '../../utils/status';
import { HumanActionCard } from './HumanActionCard';

const { Title } = Typography;

/** 运行中 run 的阶段映射（事件 stage → Steps 序号）。 */
function runStageIndex(events: WorkflowEventResponse[]): number {
  const seen = new Set<string>();
  for (const event of events) {
    if (event.stage) {
      seen.add(event.stage);
    }
  }
  if (seen.has('auditing') || seen.has('checking')) {
    return 4;
  }
  if (seen.has('writing')) {
    return 3;
  }
  if (seen.has('analyzing') || seen.has('synthesizing')) {
    return 2;
  }
  if (seen.has('collecting') || seen.has('parsing') || seen.has('evidence_extraction')) {
    return 1;
  }
  return 0;
}

interface Props {
  task: TaskResponse;
  run: WorkflowRunResponse | null;
  events: WorkflowEventResponse[];
  /** 有事件/有 run 时渲染；否则显示空态。 */
  /** 编排 awaiting_stage5 时由 OrchestrationHumanActionCard 接管人工决策，
   * 此处隐藏基于 workflow-runs actions 的 HumanActionCard（避免错误 dispatch
   * 到 /workflow-runs/{id}/actions 而不继续顶层编排）。 */
  suppressHumanAction?: boolean;
}

export function WorkflowProgressPanel({
  task,
  run,
  events,
  suppressHumanAction = false,
}: Props): React.JSX.Element {
  if (!run) {
    return (
      <Card title="工作流进度">
        <Empty description="尚未启动研究执行" />
      </Card>
    );
  }

  const stageIndex = runStageIndex(events);
  const latestProgress =
    events.length > 0 ? (events[events.length - 1].progress ?? task.progress) : task.progress;

  return (
    <Card title="工作流进度">
      <Space direction="vertical" style={{ width: '100%' }} size="middle">
        <Descriptions size="small" column={3}>
          <Descriptions.Item label="运行状态">
            <StatusTag kind="run" status={run.status} />
          </Descriptions.Item>
          <Descriptions.Item label="图">{run.graph_name}</Descriptions.Item>
          <Descriptions.Item label="版本">{run.graph_version}</Descriptions.Item>
          <Descriptions.Item label="当前阶段">{stageLabel(task.current_stage)}</Descriptions.Item>
          <Descriptions.Item label="待处理操作">{run.pending_action ?? '—'}</Descriptions.Item>
          <Descriptions.Item label="任务进度">{latestProgress}%</Descriptions.Item>
        </Descriptions>

        <Progress percent={latestProgress} status={run.status === 'failed' ? 'exception' : undefined} />

        <Steps
          size="small"
          current={stageIndex}
          items={[
            { title: '计划' },
            { title: '收集' },
            { title: '分析' },
            { title: '撰写' },
            { title: '审核' },
          ]}
        />

        {run.status === 'failed' ? (
          <Alert
            type="error"
            showIcon
            message="运行失败"
            description={
              <span>
                {run.error_code ? <strong>{run.error_code}：</strong> : null}
                {run.error_message ?? '无错误信息'}
              </span>
            }
          />
        ) : null}

        {!suppressHumanAction && (run.status === 'waiting_human' || run.pending_action) ? (
          <HumanActionCard run={run} />
        ) : null}

        <div>
          <Title level={5}>事件时间线</Title>
          <EventTimeline events={events} />
        </div>
      </Space>
    </Card>
  );
}
