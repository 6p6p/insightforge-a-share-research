/** useTaskEvents 真正终态关闭行为（spec D）。

用 Fake EventSource 证明四条语义：
1. 临时 Stage4 completion（后台链仍在 Stage4→Stage5 过渡）**不会**永久关闭流；
2. 最终 Stage5 completion（链已退出）只关闭一次；
3. 组件 unmount 关闭 EventSource（不残留连接）；
4. 断线 / 重连不创建重复 EventSource；已终态任务由 onerror 兜底关闭。

核心不变量：原生 EventSource 在服务端 EOF / 断线后进入 CONNECTING 自动重连，
**不会**自然到达 CLOSED。客户端只能通过「terminal run event → workspace 二次
确认（current_run 终态 && research_chain_active=false）」决定 close。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, screen } from '@testing-library/react';

import { renderWithProviders } from '../test/render';
import { useTaskEvents } from './useTaskEvents';
import type { WorkflowRunStatus } from '../types/workflow';

const mocks = vi.hoisted(() => ({
  taskEventSourceUrl: vi.fn(),
  getTaskWorkspace: vi.fn(),
}));

vi.mock('../api/events', () => ({
  taskEventSourceUrl: mocks.taskEventSourceUrl,
}));

vi.mock('../api/tasks', () => ({
  taskKeys: {
    all: ['tasks'],
    list: () => ['tasks', 'list'],
    detail: (id: string) => ['tasks', 'detail', id],
    workspace: (id: string) => ['tasks', 'workspace', id],
  },
  getTaskWorkspace: mocks.getTaskWorkspace,
}));

type FakeEventHandler = (event: { data: string; lastEventId: string | null }) => void;

/** 模拟浏览器 EventSource：记录实例数 / close 次数，供测试精确断言。 */
class FakeEventSource {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 2;
  static instances: FakeEventSource[] = [];

  readonly url: string;
  readyState: number = FakeEventSource.CONNECTING;
  closeCount = 0;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  private handlers = new Map<string, Set<FakeEventHandler>>();

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, handler: FakeEventHandler): void {
    const set = this.handlers.get(type) ?? new Set();
    set.add(handler);
    this.handlers.set(type, set);
  }

  close(): void {
    this.closeCount += 1;
    this.readyState = FakeEventSource.CLOSED;
  }

  // ---- 测试辅助：模拟浏览器 / 服务器行为 ----

  open(): void {
    this.readyState = FakeEventSource.OPEN;
    this.onopen?.();
  }

  /** 服务端 EOF / 断线 → 原生 EventSource 进入 CONNECTING 并触发 onerror。 */
  disconnect(): void {
    this.readyState = FakeEventSource.CONNECTING;
    this.onerror?.();
  }

  emit(type: string, data: string, lastEventId?: string): void {
    const event = { data, lastEventId: lastEventId ?? null };
    for (const handler of this.handlers.get(type) ?? []) {
      handler(event);
    }
  }

  static reset(): void {
    FakeEventSource.instances = [];
  }
}

/** 生成合法的 SSE data（frameToWorkflowEvent 要求 event_id / event_type 存在）。 */
function eventData(event_id: number, event_type: string): string {
  return JSON.stringify({
    event_id,
    run_id: 'run-1',
    event_type,
    node_name: null,
    stage: 'stage4',
    progress: 50,
    message: 'test',
    payload: {},
    created_at: '2026-08-11T00:00:00Z',
  });
}

/** workspace 投影的极小 mock（hook 只读 current_run.status + research_chain_active）。 */
function workspaceMock(status: WorkflowRunStatus, research_chain_active: boolean): unknown {
  return { current_run: { run_id: 'run-1', status }, research_chain_active };
}

/** 冲刷 TanStack Query notifyManager 的异步批量通知（setTimeout / queueMicrotask），
使 checkTerminalAndClose 触发的 setState 落在 act 内，避免 act 警告。 */
async function flushAsync(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

function Harness({ taskId }: { taskId: string | null }): React.JSX.Element {
  const { connected, streamEnded, error } = useTaskEvents(taskId);
  return (
    <div>
      <span data-testid="connected">{String(connected)}</span>
      <span data-testid="streamEnded">{String(streamEnded)}</span>
      <span data-testid="error">{error ?? ''}</span>
    </div>
  );
}

beforeEach(() => {
  FakeEventSource.reset();
  vi.stubGlobal('EventSource', FakeEventSource);
  mocks.taskEventSourceUrl.mockReset();
  mocks.taskEventSourceUrl.mockReturnValue('http://test/api/v1/tasks/task-1/events');
  mocks.getTaskWorkspace.mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('useTaskEvents 终态关闭（spec D）', () => {
  it('临时 Stage4 completion（链仍在 Stage4→Stage5 过渡）不关闭流', async () => {
    // Stage4 完成事件到达时 workspace 显示：current_run=completed 但研究链仍 active。
    mocks.getTaskWorkspace.mockResolvedValue(workspaceMock('completed', true));

    renderWithProviders(<Harness taskId="task-1" />);
    const source = FakeEventSource.instances[0];
    expect(source).toBeDefined();
    act(() => source.open());

    await act(async () => {
      source.emit('run_completed', eventData(1, 'run_completed'));
    });
    // 冲刷 checkTerminalAndClose 的 fetchQuery 通知，使其继续执行落在 act 内。
    await flushAsync();

    // 不关闭、不标记 streamEnded、不报重连错误。
    expect(source.closeCount).toBe(0);
    expect(screen.getByTestId('streamEnded').textContent).toBe('false');
    expect(screen.getByTestId('error').textContent).toBe('');

    // 过渡到 Stage5 运行中（current_run=running、链仍 active）→ 依旧不关闭。
    mocks.getTaskWorkspace.mockResolvedValue(workspaceMock('running', true));
    await act(async () => {
      source.emit('run_started', eventData(2, 'run_started'));
    });
    expect(source.closeCount).toBe(0);
    expect(screen.getByTestId('streamEnded').textContent).toBe('false');
  });

  it('最终 Stage5 completion（链已退出）只关闭一次', async () => {
    mocks.getTaskWorkspace.mockResolvedValue(workspaceMock('completed', false));

    renderWithProviders(<Harness taskId="task-1" />);
    const source = FakeEventSource.instances[0];
    act(() => source.open());

    await act(async () => {
      source.emit('run_completed', eventData(3, 'run_completed'));
    });

    // workspace 确认真正终态 → 主动 close + streamEnded。
    await flushAsync();
    expect(source.closeCount).toBe(1);
    expect(screen.getByTestId('streamEnded').textContent).toBe('true');

    // 后续 onerror 不再重复 close、不新建 EventSource。
    await act(async () => {
      source.disconnect();
    });
    expect(source.closeCount).toBe(1);
    expect(FakeEventSource.instances).toHaveLength(1);
  });

  it('组件 unmount 关闭 EventSource（不残留连接）', () => {
    const { unmount } = renderWithProviders(<Harness taskId="task-1" />);
    const source = FakeEventSource.instances[0];
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(source.closeCount).toBe(0);

    unmount();
    expect(source.closeCount).toBe(1);
    expect(FakeEventSource.instances).toHaveLength(1);
  });

  it('断线重连不创建重复 EventSource；已终态任务由 onerror 兜底关闭', async () => {
    // 运行中：断线 → 仅同一实例自动重连，不新建、不误关闭。
    mocks.getTaskWorkspace.mockResolvedValue(workspaceMock('running', true));

    renderWithProviders(<Harness taskId="task-1" />);
    const source = FakeEventSource.instances[0];
    act(() => source.open());

    await act(async () => {
      source.disconnect();
    });
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(source.closeCount).toBe(0);
    expect(screen.getByTestId('connected').textContent).toBe('false');
    expect(screen.getByTestId('error').textContent).toContain('连接中断');

    // 重连成功 → connected 恢复、错误提示清除。
    await act(async () => {
      source.open();
    });
    expect(screen.getByTestId('connected').textContent).toBe('true');
    expect(screen.getByTestId('error').textContent).toBe('');

    // 页面刷新后首连即遇已终态任务：onerror(CONNECTING) → workspace 确认 → 关闭。
    mocks.getTaskWorkspace.mockResolvedValue(workspaceMock('completed', false));
    await act(async () => {
      source.disconnect();
    });
    await flushAsync();
    expect(source.closeCount).toBe(1);
    expect(screen.getByTestId('streamEnded').textContent).toBe('true');
    expect(screen.getByTestId('error').textContent).toBe('');
    expect(FakeEventSource.instances).toHaveLength(1);
  });
});
