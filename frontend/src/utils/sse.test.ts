import { describe, expect, it } from 'vitest';

import type { WorkflowEventResponse } from '../types/workflow';
import {
  createTaskEventState,
  frameToWorkflowEvent,
  parseSseFrames,
  reduceTaskEvents,
} from './sse';

/** 与后端 app/services/sse_service.py format_sse_event 完全一致的帧。 */
const BACKEND_FRAME =
  'id: 3\nevent: run_waiting_human\ndata: {"event_id":3,"run_id":"r1","event_type":"run_waiting_human","node_name":null,"stage":"human_review","progress":70,"message":"等待人工确认","payload":{},"created_at":"2026-08-11T00:00:00Z"}\n\n';

describe('sse 帧解析（与后端帧格式对齐）', () => {
  it('parseSseFrames 解析 id/event/data 字段并忽略注释与 keep-alive', () => {
    const text =
      ': connected\n\n' + BACKEND_FRAME + ': keep-alive\n\nevent: run_completed\ndata: {"event_id":4}\nid: 4\n\n';
    const frames = parseSseFrames(text);
    expect(frames).toHaveLength(2);
    expect(frames[0].id).toBe('3');
    expect(frames[0].event).toBe('run_waiting_human');
    expect(frames[0].data[0]).toContain('"event_id":3');
    expect(frames[1].event).toBe('run_completed');
    expect(frames[1].id).toBe('4');
  });

  it('frameToWorkflowEvent 返回事件；脏帧 / message 默认事件返回 null', () => {
    const frames = parseSseFrames(BACKEND_FRAME);
    const event = frameToWorkflowEvent(frames[0]);
    expect(event).not.toBeNull();
    expect(event!.event_type).toBe('run_waiting_human');
    expect(event!.progress).toBe(70);

    expect(frameToWorkflowEvent({ event: 'message', data: ['{"x":1}'], id: null })).toBeNull();
    expect(frameToWorkflowEvent({ event: 'run_created', data: ['not-json'], id: null })).toBeNull();
  });
});

function makeEvent(event_id: number, event_type: string): WorkflowEventResponse {
  return {
    event_id,
    run_id: 'r1',
    event_type: event_type as WorkflowEventResponse['event_type'],
    node_name: null,
    stage: null,
    progress: null,
    message: `事件 ${event_id}`,
    payload: {},
    created_at: '2026-08-11T00:00:00Z',
  };
}

describe('事件流 reducer / 缓存逻辑（spec L）', () => {
  it('createTaskEventState 按 event_id 排序并计算 latestId', () => {
    const state = createTaskEventState([makeEvent(3, 'run_completed'), makeEvent(1, 'run_created')]);
    expect(state.events.map((e) => e.event_id)).toEqual([1, 3]);
    expect(state.latestId).toBe(3);
  });

  it('reduceTaskEvents 按 event_id 去重合并（页面刷新重放不重复）', () => {
    const base = createTaskEventState([makeEvent(1, 'run_created'), makeEvent(2, 'run_started')]);
    // 刷新后服务端从 0 重放 1..3 + 本轮新增 3。
    const next = reduceTaskEvents(base, [
      makeEvent(1, 'run_created'),
      makeEvent(2, 'run_started'),
      makeEvent(3, 'run_waiting_human'),
    ]);
    expect(next.events.map((e) => e.event_id)).toEqual([1, 2, 3]);
    expect(next.latestId).toBe(3);
  });

  it('乱序到达的事件仍保持递增排序', () => {
    const base = createTaskEventState([makeEvent(5, 'run_completed')]);
    const next = reduceTaskEvents(base, [makeEvent(4, 'node_completed'), makeEvent(3, 'run_started')]);
    expect(next.events.map((e) => e.event_id)).toEqual([3, 4, 5]);
    expect(next.latestId).toBe(5);
  });
});
