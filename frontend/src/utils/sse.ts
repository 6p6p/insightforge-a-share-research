/** SSE 帧解析 + task 事件流 reducer（spec L）。

- 纯函数，便于 Vitest 覆盖「reducer / 缓存逻辑」。
- 与后端 /app/services/sse_service.py 的帧格式对齐：
  `id: {event_id}\nevent: {event_type}\ndata: {json}\n\n`。
- 事件按 event_id 去重合并（页面刷新后首连会从 0 重放全量历史）。
 */

import type { WorkflowEventResponse, WorkflowEventType } from '../types/workflow';

export const TASK_EVENTS_STORAGE_PREFIX = 'insightforge:task-events:';

/** 解析一段 SSE 文本流为帧数组（忽略注释行 / keep-alive 等无 data 帧）。 */
export function parseSseFrames(text: string): SseFrame[] {
  const frames: SseFrame[] = [];
  for (const block of text.split(/\r?\n\r?\n/)) {
    if (!block.trim()) {
      continue;
    }
    const frame: SseFrame = { event: 'message', data: [], id: null };
    for (const line of block.split(/\r?\n/)) {
      if (!line || line.startsWith(':')) {
        continue;
      }
      const sep = line.indexOf(':');
      const field = sep === -1 ? line : line.slice(0, sep);
      const value = sep === -1 ? '' : line.slice(sep + 1).replace(/^ /, '');
      if (field === 'event') {
        frame.event = value;
      } else if (field === 'data') {
        frame.data.push(value);
      } else if (field === 'id') {
        frame.id = value;
      }
    }
    // 纯注释块（如 : connected / : keep-alive）不产生事件，直接丢弃。
    if (frame.data.length === 0 && frame.id === null) {
      continue;
    }
    frames.push(frame);
  }
  return frames;
}

export interface SseFrame {
  event: string;
  data: string[];
  id: string | null;
}

/** 将单个 frame 转成 WorkflowEventResponse；解析失败返回 null（跳过脏帧）。 */
export function frameToWorkflowEvent(frame: SseFrame): WorkflowEventResponse | null {
  if (frame.data.length === 0 || frame.event === 'message') {
    return null;
  }
  const raw = frame.data.join('\n');
  try {
    const parsed = JSON.parse(raw) as WorkflowEventResponse;
    if (typeof parsed.event_id !== 'number' || typeof parsed.event_type !== 'string') {
      return null;
    }
    return parsed;
  } catch {
    return null;
  }
}

/** 事件流状态：按 event_id 递增合并，latestId 供断线重连续传。 */
export interface TaskEventState {
  events: WorkflowEventResponse[];
  latestId: number | null;
}

export function createTaskEventState(initial?: WorkflowEventResponse[]): TaskEventState {
  const events = [...(initial ?? [])];
  events.sort((a, b) => a.event_id - b.event_id);
  return { events, latestId: events.length ? events[events.length - 1].event_id : null };
}

/** 合并新事件（去重 + 保持递增），返回新状态。纯函数。 */
export function reduceTaskEvents(
  state: TaskEventState,
  incoming: WorkflowEventResponse[],
): TaskEventState {
  const byId = new Map<number, WorkflowEventResponse>();
  for (const event of state.events) {
    byId.set(event.event_id, event);
  }
  for (const event of incoming) {
    byId.set(event.event_id, event);
  }
  const events = [...byId.values()].sort((a, b) => a.event_id - b.event_id);
  return { events, latestId: events.length ? events[events.length - 1].event_id : null };
}

/** 事件类型 → 中文说明（用于 Timeline 展示）。 */
export const EVENT_TYPE_LABEL: Record<WorkflowEventType, string> = {
  run_created: '运行已创建',
  run_started: '运行已启动',
  node_completed: '节点完成',
  run_completed: '运行已完成',
  run_failed: '运行失败',
  run_waiting_human: '等待人工确认',
  run_resumed: '运行已恢复',
  run_cancelled: '运行已取消',
};

export function eventTypeLabel(type: WorkflowEventType): string {
  return EVENT_TYPE_LABEL[type] ?? type;
}
