/** useTaskEvents(taskId)（spec L）。

- EventSource 自动连接 task 级 SSE；
- 断线重连由浏览器原生处理（自动带 `Last-Event-ID`，后端续传不丢事件）；
- 页面刷新首连无 Last-Event-ID → 后端从 0 重放全量历史，配合 localStorage
  预填充实现「刷新不丢历史」；
- 收到事件：增量并入 reducer 并失效 TanStack Query workspace 缓存；
- 组件 unmount：close EventSource（避免重复 connection）。

流正常终止（任务终态、后端主动断开 → readyState CLOSED）时停止重连。
 */

import { useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';

import { taskEventSourceUrl } from '../api/events';
import { taskKeys } from '../api/tasks';
import type { WorkflowEventResponse, WorkflowEventType } from '../types/workflow';
import {
  createTaskEventState,
  frameToWorkflowEvent,
  reduceTaskEvents,
  TASK_EVENTS_STORAGE_PREFIX,
  type SseFrame,
  type TaskEventState,
} from '../utils/sse';

const WORKFLOW_EVENT_TYPES: WorkflowEventType[] = [
  'run_created',
  'run_started',
  'node_completed',
  'run_completed',
  'run_failed',
  'run_waiting_human',
  'run_resumed',
  'run_cancelled',
];

const MAX_PERSISTED_EVENTS = 200;

export interface UseTaskEventsResult {
  events: WorkflowEventResponse[];
  latestId: number | null;
  connected: boolean;
  streamEnded: boolean;
  error: string | null;
}

function loadPersisted(taskId: string): WorkflowEventResponse[] {
  try {
    const raw = localStorage.getItem(TASK_EVENTS_STORAGE_PREFIX + taskId);
    if (!raw) {
      return [];
    }
    const parsed = JSON.parse(raw) as WorkflowEventResponse[];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function persist(taskId: string, events: WorkflowEventResponse[]): void {
  try {
    const recent = events.slice(-MAX_PERSISTED_EVENTS);
    localStorage.setItem(TASK_EVENTS_STORAGE_PREFIX + taskId, JSON.stringify(recent));
  } catch {
    // localStorage 不可用（隐私模式等）时静默降级。
  }
}

/** 把浏览器 MessageEvent 还原为 SseFrame（data + 具名 event type + lastEventId）。 */
function toFrame(event: MessageEvent, type: string): SseFrame {
  const data = typeof event.data === 'string' ? event.data : '';
  return { event: type, data: [data], id: event.lastEventId || null };
}

export function useTaskEvents(taskId: string | null): UseTaskEventsResult {
  const queryClient = useQueryClient();
  const [state, setState] = useState<TaskEventState>(() =>
    createTaskEventState(taskId ? loadPersisted(taskId) : undefined),
  );
  const [connected, setConnected] = useState(false);
  const [streamEnded, setStreamEnded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const invalidateTimer = useRef<number | null>(null);

  useEffect(() => {
    if (!taskId) {
      return;
    }
    // 任务切换 / 刷新时重建状态。
    setState(createTaskEventState(loadPersisted(taskId)));
    setConnected(false);
    setStreamEnded(false);
    setError(null);

    let source: EventSource | null = null;

    const invalidateWorkspace = (): void => {
      // 轻量节流：事件突发时合并为一次失效。
      if (invalidateTimer.current !== null) {
        window.clearTimeout(invalidateTimer.current);
      }
      invalidateTimer.current = window.setTimeout(() => {
        invalidateTimer.current = null;
        void queryClient.invalidateQueries({ queryKey: taskKeys.workspace(taskId) });
      }, 300);
    };

    source = new EventSource(taskEventSourceUrl(taskId));
    source.onopen = (): void => setConnected(true);

    for (const type of WORKFLOW_EVENT_TYPES) {
      source.addEventListener(type, (rawEvent: Event) => {
        const frame = toFrame(rawEvent as MessageEvent, type);
        const event = frameToWorkflowEvent(frame);
        if (!event) {
          return;
        }
        setState((prev) => {
          const next = reduceTaskEvents(prev, [event]);
          persist(taskId, next.events);
          return next;
        });
        invalidateWorkspace();
      });
    }

    source.onerror = (): void => {
      setConnected(false);
      if (source && source.readyState === EventSource.CLOSED) {
        // 后端正常结束流（任务终态）→ 不再重连。
        setStreamEnded(true);
        source.close();
      } else if (source && source.readyState === EventSource.CONNECTING) {
        setError('连接中断，正在重连…');
      }
    };

    return () => {
      if (invalidateTimer.current !== null) {
        window.clearTimeout(invalidateTimer.current);
        invalidateTimer.current = null;
      }
      source?.close();
    };
  }, [taskId, queryClient]);

  return {
    events: state.events,
    latestId: state.latestId,
    connected,
    streamEnded,
    error,
  };
}
