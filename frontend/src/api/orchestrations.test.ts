import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./client')>();
  return { ...actual, apiRequest: vi.fn() };
});

import { apiRequest } from './client';
import {
  actOnOrchestration,
  createOrchestration,
  getCurrentOrchestration,
  orchestrationKeys,
  resumeSourceAcquisition,
} from './orchestrations';

const mockedApiRequest = vi.mocked(apiRequest);

describe('orchestration API（7A Product Gate spec M/N）', () => {
  beforeEach(() => {
    mockedApiRequest.mockReset();
  });

  it('orchestrationKeys 覆盖 current/detail', () => {
    expect(orchestrationKeys.current('t1')).toEqual(['orchestrations', 'current', 't1']);
    expect(orchestrationKeys.detail('orch-1')).toEqual(['orchestrations', 'detail', 'orch-1']);
  });

  it('createOrchestration → POST /tasks/{task}/orchestrations（一键入口）', async () => {
    mockedApiRequest.mockResolvedValue({ orchestration_id: 'orch-1' });
    await createOrchestration('t1');
    expect(mockedApiRequest).toHaveBeenCalledWith('/tasks/t1/orchestrations', {
      method: 'POST',
    });
  });

  it('getCurrentOrchestration → GET /tasks/{task}/orchestrations/current', async () => {
    mockedApiRequest.mockResolvedValue({ orchestration_id: 'orch-1' });
    await getCurrentOrchestration('t1');
    expect(mockedApiRequest).toHaveBeenCalledWith('/tasks/t1/orchestrations/current');
  });

  it('resumeSourceAcquisition → POST /research-orchestrations/{id}/resume-source-acquisition', async () => {
    mockedApiRequest.mockResolvedValue({ orchestration_id: 'orch-1' });
    await resumeSourceAcquisition('orch-1');
    expect(mockedApiRequest).toHaveBeenCalledWith(
      '/research-orchestrations/orch-1/resume-source-acquisition',
      { method: 'POST' },
    );
  });

  it('actOnOrchestration → POST /research-orchestrations/{id}/actions + {action, comment}', async () => {
    mockedApiRequest.mockResolvedValue({ orchestration_id: 'orch-1' });
    await actOnOrchestration('orch-1', 'approve', '同意');
    expect(mockedApiRequest).toHaveBeenCalledWith('/research-orchestrations/orch-1/actions', {
      method: 'POST',
      body: { action: 'approve', comment: '同意' },
    });
  });

  it('actOnOrchestration 空 comment → null', async () => {
    mockedApiRequest.mockResolvedValue({ orchestration_id: 'orch-1' });
    await actOnOrchestration('orch-1', 'cancel');
    expect(mockedApiRequest).toHaveBeenCalledWith('/research-orchestrations/orch-1/actions', {
      method: 'POST',
      body: { action: 'cancel', comment: null },
    });
  });
});
