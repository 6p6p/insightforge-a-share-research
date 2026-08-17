/** 研究事件时间线（V1.1 产品语义；不暴露 LangGraph 节点名）。 */

import { Timeline } from 'antd';

import type { WorkflowEventResponse } from '../types/workflow';
import { eventTypeLabel } from '../utils/sse';
import { stageLabel } from '../utils/status';

function formatTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return iso;
  }
  return date.toLocaleString('zh-CN', { hour12: false });
}

function eventColor(event: WorkflowEventResponse): string {
  switch (event.event_type) {
    case 'run_completed':
      return 'green';
    case 'run_failed':
    case 'run_cancelled':
      return 'red';
    case 'run_waiting_human':
      return 'orange';
    case 'run_created':
    case 'run_started':
    case 'node_completed':
    case 'run_resumed':
      return 'blue';
  }
}

interface Props {
  events: WorkflowEventResponse[];
  emptyText?: string;
}

export function EventTimeline({ events, emptyText = '暂无事件' }: Props): React.JSX.Element {
  if (events.length === 0) {
    return <span style={{ color: 'rgba(0,0,0,0.45)' }}>{emptyText}</span>;
  }
  const items = [...events]
    .sort((a, b) => a.event_id - b.event_id)
    .map((event) => ({
      key: event.event_id,
      color: eventColor(event),
      children: (
        <div>
          <div>
            <strong>{eventTypeLabel(event.event_type)}</strong>
            {event.stage ? <span style={{ marginLeft: 8 }}>阶段：{stageLabel(event.stage)}</span> : null}
          </div>
          <div style={{ color: 'rgba(0,0,0,0.65)' }}>{event.message}</div>
          {event.node_name ? <div style={{ color: 'rgba(0,0,0,0.45)' }}>步骤：{event.node_name}</div> : null}
          <div style={{ color: 'rgba(0,0,0,0.45)', fontSize: 12 }}>{formatTime(event.created_at)}</div>
        </div>
      ),
    }));
  return <Timeline items={items} />;
}
