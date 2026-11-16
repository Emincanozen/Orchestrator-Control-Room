import { describe, expect, it } from 'vitest';
import { ev, mkAgent, mkApproval, mkProject, mkTask } from '../test/fixtures';
import {
  activeSeconds,
  applyEvent,
  applyEvents,
  applySnapshot,
  initialData,
  pendingApprovals,
  runningTaskCount,
  resolveClock,
  seedLog,
  unresolvedClocks,
  upsertTask,
  type StoreData,
} from './reducer';

const snap = (over: Partial<Parameters<typeof applySnapshot>[1]> = {}): StoreData =>
  applySnapshot(initialData(), {
    projects: [mkProject()],
    tasks: [mkTask({ id: 'tsk_1', state: 'QUEUED' })],
    agents: [mkAgent()],
    approvals: [],
    lastSeq: 10,
    ...over,
  });

describe('snapshot', () => {
  it('loads entities and sets the resume seq', () => {
    const s = snap();
    expect(s.loaded).toBe(true);
    expect(s.lastSeq).toBe(10);
    expect(Object.keys(s.tasks)).toEqual(['tsk_1']);
  });
});

describe('applyEvent ordering', () => {
  it('ignores duplicate and out-of-order seq', () => {
    let s = snap();
    s = applyEvent(s, ev(11, 'task.state_changed', { task_id: 'tsk_1', payload: { from: 'QUEUED', to: 'RUNNING' } }));
    expect(s.tasks.tsk_1.state).toBe('RUNNING');
    expect(s.lastSeq).toBe(11);
    const dup = applyEvent(s, ev(11, 'task.state_changed', { task_id: 'tsk_1', payload: { to_state: 'FAILED' } }));
    expect(dup).toBe(s);
    const old = applyEvent(s, ev(5, 'task.state_changed', { task_id: 'tsk_1', payload: { to_state: 'FAILED' } }));
    expect(old).toBe(s);
    expect(old.tasks.tsk_1.state).toBe('RUNNING');
  });

  it('ignores events at or below the snapshot seq (replay after reconnect)', () => {
    const s = snap();
    const out = applyEvents(s, [ev(9, 'task.state_changed', { task_id: 'tsk_1', payload: { to_state: 'FAILED' } }), ev(10, 'task.updated', { task_id: 'tsk_1' })]);
    expect(out).toBe(s);
  });

  it('accepts a gap (seq jumps forward)', () => {
    const s = applyEvent(snap(), ev(40, 'system.notice', { payload: { message: 'hi' } }));
    expect(s.lastSeq).toBe(40);
    expect(s.notices[0].message).toBe('hi');
  });
});

describe('task events', () => {
  it('task.created with a full task inserts it', () => {
    const t = mkTask({ id: 'tsk_2', title: 'New' });
    const s = applyEvent(snap(), ev(11, 'task.created', { task_id: 'tsk_2', payload: { task: t } }));
    expect(s.tasks.tsk_2.title).toBe('New');
  });

  it('task.created without a task body marks the id stale (no invented data)', () => {
    const s = applyEvent(snap(), ev(11, 'task.created', { task_id: 'tsk_9' }));
    expect(s.tasks.tsk_9).toBeUndefined();
    expect(s.stale.tasks).toContain('tsk_9');
  });

  it('task.updated carries field names only, so it queues a refetch', () => {
    const s = applyEvent(snap(), ev(11, 'task.updated', { task_id: 'tsk_1', payload: { fields: ['title', 'priority'], actor: 'user' } }));
    expect(s.tasks.tsk_1.title).toBe('Write parser');
    expect(s.stale.tasks).toContain('tsk_1');
  });

  it('state change records reason and counts running tasks', () => {
    const s = applyEvent(snap(), ev(11, 'task.state_changed', { task_id: 'tsk_1', payload: { from: 'QUEUED', to: 'RUNNING', reason: 'picked', actor: 'scheduler' } }));
    expect(s.tasks.tsk_1.state_reason).toBe('picked');
    expect(runningTaskCount(s)).toBe(1);
  });

  it('active duration accumulates across active segments from recorded timestamps', () => {
    let s = snap();
    s = applyEvent(s, ev(11, 'task.state_changed', { ts: '2026-11-11T10:00:10Z', task_id: 'tsk_1', payload: { from: 'QUEUED', to: 'RUNNING' } }));
    const t0 = Date.parse('2026-11-11T10:00:10Z');
    expect(activeSeconds(s.clocks.tsk_1, s.tasks.tsk_1, t0 + 5000)).toBeCloseTo(5);
    s = applyEvent(s, ev(12, 'task.state_changed', { ts: '2026-11-11T10:00:40Z', task_id: 'tsk_1', payload: { from: 'RUNNING', to: 'PAUSED' } }));
    // paused: frozen at 30s
    expect(activeSeconds(s.clocks.tsk_1, s.tasks.tsk_1, t0 + 999_000)).toBeCloseTo(30);
    s = applyEvent(s, ev(13, 'task.state_changed', { ts: '2026-11-11T10:01:00Z', task_id: 'tsk_1', payload: { from: 'PAUSED', to: 'RUNNING' } }));
    expect(activeSeconds(s.clocks.tsk_1, s.tasks.tsk_1, Date.parse('2026-11-11T10:01:10Z'))).toBeCloseTo(40);
  });

  it('state change for an unknown task queues a refetch', () => {
    const s = applyEvent(snap(), ev(11, 'task.state_changed', { task_id: 'tsk_x', payload: { to: 'RUNNING' } }));
    expect(s.stale.tasks).toContain('tsk_x');
  });
});

describe('run output', () => {
  it('appends log lines per task and does not bump taskRev', () => {
    let s = snap();
    s = applyEvent(s, ev(11, 'run.output', { task_id: 'tsk_1', run_id: 'run_1', payload: { text: 'hello' } }));
    s = applyEvent(s, ev(12, 'run.tool_use', { task_id: 'tsk_1', run_id: 'run_1', payload: { tool: 'Edit', summary: 'a.ts' } }));
    expect(s.logs.tsk_1.map((l) => l.kind)).toEqual(['output', 'tool']);
    expect(s.logs.tsk_1[1].tool).toBe('Edit');
    expect(s.taskRev.tsk_1).toBeUndefined();
    expect(s.tasks.tsk_1.current_operation).toBe('Edit');
  });

  it('seedLog merges history without duplicates', () => {
    let s = snap();
    s = applyEvent(s, ev(12, 'run.output', { task_id: 'tsk_1', payload: { text: 'live' } }));
    s = seedLog(s, 'tsk_1', [ev(11, 'run.output', { task_id: 'tsk_1', payload: { text: 'old' } }), ev(12, 'run.output', { task_id: 'tsk_1', payload: { text: 'live' } })]);
    expect(s.logs.tsk_1.map((l) => l.seq)).toEqual([11, 12]);
  });
});

describe('agents and approvals', () => {
  it('agent.activity_changed updates the seat', () => {
    const s = applyEvent(snap(), ev(11, 'agent.activity_changed', { agent_id: 'agt_1', task_id: 'tsk_1', payload: { from: 'idle', to: 'coding', operation: 'editing x.ts', provider: 'mock', model: 'mock-model' } }));
    expect(s.agents.agt_1.activity).toBe('coding');
    expect(s.agents.agt_1.current_operation).toBe('editing x.ts');
    expect(s.agentEvents.agt_1).toHaveLength(1);
  });

  it('approval requested then resolved updates the pending list', () => {
    let s = applyEvent(snap(), ev(11, 'approval.requested', { payload: { approval: mkApproval(), auto_approved: false } }));
    expect(pendingApprovals(s)).toHaveLength(1);
    s = applyEvent(s, ev(12, 'approval.resolved', { payload: { approval_id: 'apr_1', kind: 'destructive_git', approved: true, by: 'user' } }));
    expect(pendingApprovals(s)).toHaveLength(0);
    expect(s.approvals.apr_1.status).toBe('approved');
  });

  it('leader.changed updates the leader', () => {
    const s = applyEvent(snap(), ev(11, 'leader.changed', { payload: { provider: 'codex', model: 'gpt' } }));
    expect(s.leader).toEqual({ provider: 'codex', model: 'gpt' });
  });

  it('verification.result records status', () => {
    const s = applyEvent(snap(), ev(11, 'verification.result', { task_id: 'tsk_1', payload: { status: 'passed' } }));
    expect(s.verification.tsk_1).toBe('passed');
  });
});

describe('active duration for a task already active at snapshot time', () => {
  const T0 = Date.parse('2026-11-11T10:00:00Z');
  const active = () =>
    snap({ tasks: [mkTask({ id: 'tsk_1', state: 'RUNNING', active_seconds: 20, last_activity_at: '2026-11-11T10:09:59Z' })] });

  it('is not ticking from the snapshot instant: the running segment start is unknown until resolved', () => {
    const s = active();
    expect(unresolvedClocks(s)).toEqual(['tsk_1']);
    expect(activeSeconds(s.clocks.tsk_1, s.tasks.tsk_1, T0 + 60_000)).toBe(20);
  });

  it('resolves from the last transition ts (not last_activity_at, which heartbeats overwrite)', () => {
    let s = active();
    s = resolveClock(s, 'tsk_1', [
      { id: 1, task_id: 'tsk_1', from_state: 'CREATED', to_state: 'QUEUED', reason: null, actor: 'user', ts: '2026-11-11T09:00:00Z' },
      { id: 2, task_id: 'tsk_1', from_state: 'QUEUED', to_state: 'RUNNING', reason: null, actor: 'scheduler', ts: '2026-11-11T09:59:30Z' },
    ]);
    expect(unresolvedClocks(s)).toEqual([]);
    expect(activeSeconds(s.clocks.tsk_1, s.tasks.tsk_1, T0)).toBeCloseTo(50); // 20 stored + 30 running
  });

  it('ignores transitions that do not end in the current state', () => {
    const s = resolveClock(active(), 'tsk_1', [{ id: 1, task_id: 'tsk_1', from_state: 'RUNNING', to_state: 'TESTING', reason: null, actor: 'x', ts: '2026-11-11T09:59:30Z' }]);
    expect(unresolvedClocks(s)).toEqual(['tsk_1']);
  });

  it('leaving an active state refetches the row so the stored active_seconds wins', () => {
    let s = resolveClock(active(), 'tsk_1', [{ id: 2, task_id: 'tsk_1', from_state: 'QUEUED', to_state: 'RUNNING', reason: null, actor: 'x', ts: '2026-11-11T09:59:30Z' }]);
    s = applyEvent(s, ev(11, 'task.state_changed', { ts: '2026-11-11T10:00:10Z', task_id: 'tsk_1', payload: { from: 'RUNNING', to: 'FAILED', reason: 'boom', actor: 'executor' } }));
    expect(s.stale.tasks).toContain('tsk_1');
    expect(s.tasks.tsk_1.finished_at).toBe('2026-11-11T10:00:10Z');
    expect(activeSeconds(s.clocks.tsk_1, s.tasks.tsk_1, T0 + 999_000)).toBeCloseTo(60); // 20 + 40
  });

  it('active -> active keeps the pinned segment', () => {
    let s = resolveClock(active(), 'tsk_1', [{ id: 2, task_id: 'tsk_1', from_state: 'QUEUED', to_state: 'RUNNING', reason: null, actor: 'x', ts: '2026-11-11T09:59:30Z' }]);
    s = applyEvent(s, ev(11, 'task.state_changed', { ts: '2026-11-11T10:00:00Z', task_id: 'tsk_1', payload: { from: 'RUNNING', to: 'TESTING' } }));
    expect(activeSeconds(s.clocks.tsk_1, s.tasks.tsk_1, T0 + 10_000)).toBeCloseTo(60);
  });

  it('a refetched row keeps client-derived fields and a pinned clock', () => {
    let s = resolveClock(active(), 'tsk_1', [{ id: 2, task_id: 'tsk_1', from_state: 'QUEUED', to_state: 'RUNNING', reason: null, actor: 'x', ts: '2026-11-11T09:59:30Z' }]);
    s = applyEvent(s, ev(11, 'run.started', { task_id: 'tsk_1', agent_id: 'agt_1', payload: { phase: 'execute', provider: 'mock', model: 'mock-model', idempotency_key: 'k' } }));
    s = upsertTask(s, mkTask({ id: 'tsk_1', state: 'RUNNING', active_seconds: 20 }));
    expect(s.tasks.tsk_1.provider).toBe('mock');
    expect(s.clocks.tsk_1.exact).toBe(true);
  });
});

describe('real event payloads', () => {
  it('agent.activity_changed to idle clears the seat and asks for the exact row', () => {
    let s = applyEvent(snap(), ev(11, 'agent.activity_changed', { agent_id: 'agt_1', task_id: 'tsk_1', run_id: 'run_1', payload: { from: 'idle', to: 'coding', operation: 'x', provider: 'mock', model: 'm' } }));
    expect(s.agents.agt_1.current_run_id).toBe('run_1');
    s = applyEvent(s, ev(12, 'agent.activity_changed', { agent_id: 'agt_1', payload: { from: 'coding', to: 'idle', operation: null, provider: null, model: null } }));
    expect(s.agents.agt_1.activity).toBe('idle');
    expect(s.agents.agt_1.current_task_id).toBeNull();
    expect(s.agents.agt_1.provider).toBeNull();
    expect(s.stale.agents).toContain('agt_1');
  });

  it('formats tool_use files into the log line', () => {
    const s = applyEvent(snap(), ev(11, 'run.tool_use', { task_id: 'tsk_1', payload: { tool: 'write_file', summary: 'wrote out.txt', files: ['out.txt'] } }));
    expect(s.logs.tsk_1[0].text).toBe('wrote out.txt [out.txt]');
  });
});
