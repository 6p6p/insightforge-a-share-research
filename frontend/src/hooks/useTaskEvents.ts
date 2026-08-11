/** useTaskEvents(taskId)（spec L）。

- EventSource 自动连接 task 级 SSE；
- 断线重连由浏览器原生处理（自动带 `Last-Event-ID`，后端续传不丢事件）；
- 页面刷新首连无 Last-Event-ID → 后端从 0 重放全量历史，配合 localStorage
  预填充实现「刷新不丢历史」；
- 收到事件：增量并入 reducer 并失效 TanStack Query workspace 缓存；
- 组件 unmount：close EventSource（避免重复 connection）。

**真正终态才关闭流（spec D）**：原生 EventSource 在服务端 EOF / 断线后进入
CONNECTING 自动重连，**不会**自然到达 CLOSED——不能仅凭 readyState 判断
「任务终态」。客户端在收到 terminal run event（run_completed / run_failed /
run_cancelled）后主动重查 workspace：当前 run 已是 terminal **且**后台研究链
已退出（`research_chain_active=false`，不在 Stage4→Stage5 过渡）才
`EventSource.close()` 并停止重连。临时 Stage4 completion 因链仍在运行而
**不会**被误判为终态。
 */

import { useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';

import { taskEventSourceUrl } from '../api/events';
import { getTaskWorkspace, taskKeys } from '../api/tasks';
import type { WorkflowRunStatus } from '../types/workflow';
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

/** 触发「确认是否真正终态」的 run event 类型。 */
const TERMINAL_EVENT_TYPES: WorkflowEventType[] = ['run_completed', 'run_failed', 'run_cancelled'];

/** WorkflowRun 的终态集合（与后端 domain.tasks.TERMINAL 对齐）。 */
const TERMINAL_RUN_STATUSES = new Set<WorkflowRunStatus>(['completed', 'failed', 'cancelled']);

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
    // 已确认真正终态并 close 后不再触发任何重查 / 重连逻辑。
    let closed = false;

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

    /** terminal run event → 主动重查 workspace（不走陈旧缓存）确认真正终态。

    原生 EventSource 无法仅凭 readyState 区分「任务终态正常收流」与「网络抖动」，
    因此用 workspace 二重确认：`current_run` 已是 terminal 且后台研究链已退出
    （不在 Stage4→Stage5 过渡）才 `close()`，并标记 streamEnded 停止重连。
     */
    const checkTerminalAndClose = async (currentSource: EventSource): Promise<void> => {
      if (closed) {
        return;
      }
      try {
        const workspace = await queryClient.fetchQuery({
          queryKey: taskKeys.workspace(taskId),
          queryFn: () => getTaskWorkspace(taskId),
          staleTime: 0,
        });
        const run = workspace.current_run;
        if (run !== null && TERMINAL_RUN_STATUSES.has(run.status) && !workspace.research_chain_active) {
          closed = true;
          setStreamEnded(true);
          setError(null);
          currentSource.close();
        }
      } catch {
        // workspace 查询失败（网络抖动）时保持现状，等待下一次 onerror / 事件重试。
      }
    };

    source = new EventSource(taskEventSourceUrl(taskId));
    source.onopen = (): void => {
      setConnected(true);
      // 重连成功：清除「连接中断」提示。
      setError(null);
    };

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
        if (TERMINAL_EVENT_TYPES.includes(type)) {
          void checkTerminalAndClose(source);
        }
      });
    }

    source.onerror = (): void => {
      setConnected(false);
      if (closed) {
        return;
      }
      if (source && source.readyState === EventSource.CLOSED) {
        // 显式 close 后（仅 Fake / 单测会走到）→ 不再重连。
        closed = true;
        setStreamEnded(true);
        source.close();
        return;
      }
      // CONNECTING：原生 EventSource 在服务端 EOF / 断线后进入此状态并自动重连。
      // 展示重连提示的同时用 workspace 二次确认是否真正终态（刷新后首连即遇
      // 已终态任务、或 Stage5 收尾竞态时由这里兜底关闭）。
      setError('连接中断，正在重连…');
      void checkTerminalAndClose(source);
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
