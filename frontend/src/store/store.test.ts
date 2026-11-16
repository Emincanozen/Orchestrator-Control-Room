import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const h = vi.hoisted(() => ({ opts: null as null | { onControl?: (c: unknown) => void } }));

vi.mock('./stream', () => ({
  EventStream: class {
    constructor(o: { onControl?: (c: unknown) => void }) {
      h.opts = o;
    }
    start() {}
    stop() {}
  },
}));
vi.mock('../api/endpoints', () => ({
  api: {
    status: vi.fn(),
    projects: vi.fn(),
    tasks: vi.fn(),
    agents: vi.fn(),
    approvals: vi.fn(),
    taskTransitions: vi.fn(),
  },
}));
import { api } from '../api/endpoints';
import { mkTask } from '../test/fixtures';
import { initialData } from './reducer';
import { useStore } from './store';

const m = vi.mocked(api, true);
const approval = (id: string) => ({ id, task_id: null, kind: 'x', summary: '', details: {}, status: 'pending' as const, requested_at: '', resolved_at: null, resolved_by: null, note: null });

beforeEach(() => {
  vi.clearAllMocks();
  vi.useFakeTimers();
  useStore.setState({ data: initialData() });
  m.status.mockResolvedValue({ last_seq: 500, leader: null, leader_state: 'detecting' } as never);
  m.projects.mockResolvedValue([]);
  m.tasks.mockResolvedValue([mkTask({ id: 'tsk_1' })]);
  m.agents.mockResolvedValue([]);
  m.approvals.mockResolvedValue([approval('apr_new')]);
  m.taskTransitions.mockResolvedValue([]);
});
afterEach(() => {
  useStore.getState().shutdown();
  vi.useRealTimers();
});

describe('store bootstrap and truncated replay', () => {
  it('keeps the leader state reported by /status', async () => {
    await useStore.getState().bootstrap();
    expect(useStore.getState().data.leaderState).toBe('detecting');
    expect(useStore.getState().data.leader).toBeNull();
  });

  it('refetches the snapshots on replay.truncated and drops pending approvals resolved in the gap', async () => {
    await useStore.getState().bootstrap();
    useStore.getState().putApproval(approval('apr_gone'));
    m.tasks.mockClear();
    m.approvals.mockClear();
    h.opts!.onControl!({ type: 'replay.truncated', seq: 90, requested_after_seq: 3, resume_after_seq: 90, skipped: 87, cap: 500 });
    expect(m.tasks).not.toHaveBeenCalled(); // waits for the replayed batch to be applied first
    await vi.advanceTimersByTimeAsync(500);
    expect(m.tasks).toHaveBeenCalled();
    expect(m.approvals).toHaveBeenCalled();
    expect(m.agents).toHaveBeenCalled();
    const ids = Object.keys(useStore.getState().data.approvals);
    expect(ids).toContain('apr_new');
    expect(ids).not.toContain('apr_gone');
  });

  it('does not refetch for an ordinary replay.done that replayed nothing', async () => {
    await useStore.getState().bootstrap();
    m.tasks.mockClear();
    h.opts!.onControl!({ type: 'replay.done', seq: 500 });
    await vi.advanceTimersByTimeAsync(500);
    expect(m.tasks).not.toHaveBeenCalled();
  });
});
