/**
 * Pure event-sourced state: UI = f(snapshot, events). No I/O in this file.
 *
 * Invariants:
 *  - events are applied only when `seq > lastSeq` (duplicates and out-of-order
 *    replays are ignored, which makes reconnect-with-replay safe);
 *  - when an event carries only names or ids (task.updated lists changed field
 *    names, ...) the entity id is queued in `stale` so the store layer can
 *    re-fetch the REST snapshot for it. We never invent data.
 *  - payload shapes are the real ones from backend/orchestrator (domain/events.py
 *    and the emitters): task.state_changed {from,to,reason,actor},
 *    agent.activity_changed {from,to,operation,provider,model}, ...
 */
import type {
  Agent,
  AgentActivity,
  ApiEvent,
  Approval,
  ApprovalResolvedPayload,
  Project,
  RunOutputPayload,
  RunStartedPayload,
  RunToolUsePayload,
  StateTransition,
  Task,
  TaskState,
  TaskStateChangedPayload,
  VerificationStatus,
} from '../api/types';
import { isActiveState } from '../lib/transitions';
import { parseTs } from '../lib/time';

export const MAX_LOG_LINES = 3000;
export const MAX_AGENT_EVENTS = 60;
export const MAX_RECENT = 600;

export interface LogLine {
  seq: number;
  ts: string;
  kind: 'output' | 'tool';
  run_id: string | null;
  text: string;
  stream?: string;
  tool?: string;
}

/**
 * Local ticking base: active time = seconds + (now - since)/1000 while `since` is set.
 *
 * Backend semantics (services/tasks.py transition_tx): `tasks.active_seconds` only
 * accumulates when the task LEAVES an active state (previous transition ts to now).
 * So for a task that is active right now the running segment starts at the ts of
 * its last state transition. `last_activity_at` cannot be used for that: heartbeats
 * and every run event overwrite it. A task already active in a snapshot therefore
 * has an inexact clock (`exact: false`, not ticking) until its transitions are
 * fetched (GET /tasks/{id}/transitions) and `resolveClock` pins `since`.
 */
export interface TaskClock {
  seconds: number;
  since: number | null;
  exact: boolean;
}

export interface Rev {
  usage: number;
  providers: number;
  skills: number;
  routing: number;
  knowledge: number;
  leadership: number;
  decisions: number;
  approvals: number;
}

export interface Notice {
  seq: number;
  ts: string;
  type: string;
  message: string;
  level: string;
}

export interface StoreData {
  loaded: boolean;
  lastSeq: number;
  projects: Record<string, Project>;
  tasks: Record<string, Task>;
  agents: Record<string, Agent>;
  approvals: Record<string, Approval>;
  clocks: Record<string, TaskClock>;
  logs: Record<string, LogLine[]>;
  agentEvents: Record<string, ApiEvent[]>;
  taskRev: Record<string, number>;
  verification: Record<string, VerificationStatus>;
  rev: Rev;
  leader: { provider: string; model: string | null } | null;
  /** From /status.leader_state; null = not reported (older backend). */
  leaderState: 'detecting' | 'none' | 'set' | null;
  recent: ApiEvent[];
  notices: Notice[];
  stale: { tasks: string[]; agents: string[]; approvals: string[]; projects: string[] };
}

export function initialData(): StoreData {
  return {
    loaded: false,
    lastSeq: 0,
    projects: {},
    tasks: {},
    agents: {},
    approvals: {},
    clocks: {},
    logs: {},
    agentEvents: {},
    taskRev: {},
    verification: {},
    rev: { usage: 0, providers: 0, skills: 0, routing: 0, knowledge: 0, leadership: 0, decisions: 0, approvals: 0 },
    leader: null,
    leaderState: null,
    recent: [],
    notices: [],
    stale: { tasks: [], agents: [], approvals: [], projects: [] },
  };
}

export interface Snapshot {
  projects: Project[];
  tasks: Task[];
  agents: Agent[];
  approvals: Approval[];
  lastSeq: number;
  leader?: { provider: string; model: string | null } | null;
  leaderState?: 'detecting' | 'none' | 'set' | null;
  /** unused; kept optional for callers that still pass it */
  receivedAt?: number;
}

const byId = <T extends { id: string }>(xs: T[]): Record<string, T> => Object.fromEntries(xs.map((x) => [x.id, x]));

export function clockFromTask(task: Task): TaskClock {
  return { seconds: task.active_seconds ?? 0, since: null, exact: !isActiveState(task.state) };
}

/** Pins the running segment of an active task from its transition history. */
export function resolveClock(s: StoreData, taskId: string, transitions: StateTransition[]): StoreData {
  const task = s.tasks[taskId];
  if (!task || !isActiveState(task.state)) return s;
  const last = transitions[transitions.length - 1];
  const since = last && last.to_state === task.state ? parseTs(last.ts) : null;
  if (since === null) return s;
  return { ...s, clocks: { ...s.clocks, [taskId]: { seconds: task.active_seconds ?? 0, since, exact: true } } };
}

/** Active tasks whose running segment start is not known yet. */
export function unresolvedClocks(s: StoreData): string[] {
  return Object.values(s.tasks)
    .filter((t) => isActiveState(t.state) && !(s.clocks[t.id]?.exact ?? false))
    .map((t) => t.id);
}

const CLIENT_DERIVED = ['current_operation', 'provider', 'model', 'agent_id', 'verification_status', 'usage'] as const;

/** A refetched task row never carries client-derived fields: keep what the event stream taught us. */
function keepDerived(prev: Task | undefined, next: Task): Task {
  if (!prev) return next;
  const out = { ...next } as unknown as Record<string, unknown>;
  const old = prev as unknown as Record<string, unknown>;
  for (const k of CLIENT_DERIVED) if (out[k] === undefined && old[k] !== undefined) out[k] = old[k];
  return out as unknown as Task;
}

export function applySnapshot(prev: StoreData, snap: Snapshot): StoreData {
  const tasks: Record<string, Task> = {};
  const clocks: Record<string, TaskClock> = {};
  for (const t of snap.tasks) {
    tasks[t.id] = keepDerived(prev.tasks[t.id], t);
    const prevClock = prev.clocks[t.id];
    // Keep a pinned running segment across re-bootstraps while the state is unchanged.
    clocks[t.id] =
      prevClock?.exact && prev.tasks[t.id]?.state === t.state && isActiveState(t.state)
        ? { ...prevClock, seconds: t.active_seconds ?? 0 }
        : clockFromTask(t);
  }
  return {
    ...prev,
    loaded: true,
    lastSeq: Math.max(prev.lastSeq, snap.lastSeq),
    projects: byId(snap.projects),
    tasks,
    agents: byId(snap.agents),
    approvals: byId(snap.approvals),
    clocks,
    leader: snap.leader ?? prev.leader,
    leaderState: snap.leaderState ?? (snap.leader ? 'set' : prev.leaderState),
    stale: { tasks: [], agents: [], approvals: [], projects: [] },
  };
}

/** Merge a freshly fetched entity (REST refetch of a stale id). */
export function upsertTask(s: StoreData, incoming: Task): StoreData {
  const prev = s.tasks[incoming.id];
  const task = keepDerived(prev, incoming);
  const prevClock = s.clocks[incoming.id];
  // Same active state as before and the segment is pinned: the running segment did not change.
  const keepClock = !!prevClock?.exact && !!prev && isActiveState(prev.state) && prev.state === task.state;
  return {
    ...s,
    tasks: { ...s.tasks, [task.id]: task },
    clocks: { ...s.clocks, [task.id]: keepClock ? { ...prevClock, seconds: task.active_seconds ?? 0 } : clockFromTask(task) },
    stale: { ...s.stale, tasks: s.stale.tasks.filter((i) => i !== task.id) },
  };
}
export function upsertAgent(s: StoreData, agent: Agent): StoreData {
  return { ...s, agents: { ...s.agents, [agent.id]: agent }, stale: { ...s.stale, agents: s.stale.agents.filter((i) => i !== agent.id) } };
}
export function upsertApproval(s: StoreData, a: Approval): StoreData {
  return { ...s, approvals: { ...s.approvals, [a.id]: a }, stale: { ...s.stale, approvals: s.stale.approvals.filter((i) => i !== a.id) } };
}
export function upsertProject(s: StoreData, p: Project): StoreData {
  return { ...s, projects: { ...s.projects, [p.id]: p }, stale: { ...s.stale, projects: s.stale.projects.filter((i) => i !== p.id) } };
}
export function removeProject(s: StoreData, id: string): StoreData {
  const projects = { ...s.projects };
  delete projects[id];
  return { ...s, projects };
}

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);
const str = (v: unknown): string | null => (typeof v === 'string' ? v : null);
const addStale = (list: string[], id: string): string[] => (list.includes(id) ? list : [...list, id]);

function bumpTaskRev(s: StoreData, taskId: string | null | undefined): Record<string, number> {
  if (!taskId) return s.taskRev;
  return { ...s.taskRev, [taskId]: (s.taskRev[taskId] ?? 0) + 1 };
}

function pushCapped<T>(arr: T[] | undefined, item: T, cap: number): T[] {
  const next = arr ? [...arr, item] : [item];
  return next.length > cap ? next.slice(next.length - cap) : next;
}

export function logLineFromEvent(ev: ApiEvent): LogLine | null {
  if (ev.type !== 'run.output' && ev.type !== 'run.tool_use') return null;
  if (ev.type === 'run.output') {
    const p = (ev.payload ?? {}) as Partial<RunOutputPayload>;
    return { seq: ev.seq, ts: ev.ts, kind: 'output', run_id: ev.run_id ?? null, text: str(p.text) ?? '' };
  }
  const p = (ev.payload ?? {}) as Partial<RunToolUsePayload>;
  const files = Array.isArray(p.files) && p.files.length ? ` [${p.files.join(', ')}]` : '';
  return { seq: ev.seq, ts: ev.ts, kind: 'tool', run_id: ev.run_id ?? null, text: `${str(p.summary) ?? ''}${files}`.trim(), tool: str(p.tool) ?? 'tool' };
}

/** Merge historical log events (REST) into the live log, dedup by seq, keep sorted. */
export function seedLog(s: StoreData, taskId: string, events: ApiEvent[]): StoreData {
  const incoming = events.map(logLineFromEvent).filter((l): l is LogLine => l !== null);
  if (incoming.length === 0) return s;
  const existing = s.logs[taskId] ?? [];
  const seen = new Set(existing.map((l) => l.seq));
  const merged = [...existing, ...incoming.filter((l) => !seen.has(l.seq))].sort((a, b) => a.seq - b.seq);
  const capped = merged.length > MAX_LOG_LINES ? merged.slice(merged.length - MAX_LOG_LINES) : merged;
  return { ...s, logs: { ...s.logs, [taskId]: capped } };
}

const WORKING: AgentActivity[] = ['planning', 'coding', 'researching', 'testing', 'reviewing', 'consulting'];
const FINISHED_STATES: TaskState[] = ['COMPLETED', 'CANCELLED', 'FAILED'];

function applyStateChange(s: StoreData, ev: ApiEvent): StoreData {
  const taskId = ev.task_id;
  if (!taskId) return s;
  const p = (ev.payload ?? {}) as Partial<TaskStateChangedPayload>;
  const to = str(p.to) as TaskState | null;
  const task = s.tasks[taskId];
  if (!to || !task) return { ...s, stale: { ...s.stale, tasks: addStale(s.stale.tasks, taskId) } };
  const evMs = parseTs(ev.ts) ?? Date.now();
  const clock = s.clocks[taskId] ?? clockFromTask(task);
  const wasActive = isActiveState(task.state);
  const nowActive = isActiveState(to);
  let next: TaskClock = clock;
  let refetch = typeof p.from === 'string' && p.from !== task.state; // we missed a transition
  if (nowActive && !wasActive) {
    next = { seconds: clock.exact ? clock.seconds : (task.active_seconds ?? 0), since: evMs, exact: true };
  } else if (!nowActive && wasActive) {
    // The server adds (this transition - previous transition) to active_seconds; mirror it locally
    // and refetch the row so the stored value replaces our estimate.
    next =
      clock.exact && clock.since !== null
        ? { seconds: clock.seconds + Math.max(0, (evMs - clock.since) / 1000), since: null, exact: true }
        : { seconds: clock.seconds, since: null, exact: false };
    refetch = true;
  }
  const updated: Task = {
    ...task,
    state: to,
    state_reason: str(p.reason) ?? null,
    last_activity_at: ev.ts,
    updated_at: ev.ts,
    started_at: task.started_at ?? (nowActive ? ev.ts : null),
    finished_at: FINISHED_STATES.includes(to) ? ev.ts : task.state === 'FAILED' ? null : task.finished_at,
  };
  return {
    ...s,
    tasks: { ...s.tasks, [taskId]: updated },
    clocks: { ...s.clocks, [taskId]: next },
    stale: refetch ? { ...s.stale, tasks: addStale(s.stale.tasks, taskId) } : s.stale,
  };
}

export function applyEvent(s: StoreData, ev: ApiEvent): StoreData {
  if (!Number.isInteger(ev.seq) || ev.seq <= s.lastSeq) return s; // duplicate / out-of-order
  const p = ev.payload ?? {};
  let n: StoreData = { ...s, lastSeq: ev.seq, recent: pushCapped(s.recent, ev, MAX_RECENT) };

  // High-volume streaming events must not trigger tab refetches.
  if (ev.task_id && ev.type !== 'run.output' && ev.type !== 'run.tool_use') n.taskRev = bumpTaskRev(n, ev.task_id);
  if (ev.agent_id) n.agentEvents = { ...n.agentEvents, [ev.agent_id]: pushCapped(n.agentEvents[ev.agent_id], ev, MAX_AGENT_EVENTS) };

  switch (ev.type) {
    case 'task.created': {
      const t = isObj(p.task) && str(p.task.id) ? (p.task as unknown as Task) : null;
      if (t) {
        n.tasks = { ...n.tasks, [t.id]: t };
        n.clocks = { ...n.clocks, [t.id]: clockFromTask(t) };
      } else if (ev.task_id) n.stale = { ...n.stale, tasks: addStale(n.stale.tasks, ev.task_id) };
      break;
    }
    case 'task.updated': {
      // payload is {fields: [names], actor?}: names only, so refetch the row.
      if (ev.task_id) n.stale = { ...n.stale, tasks: addStale(n.stale.tasks, ev.task_id) };
      break;
    }
    case 'task.state_changed':
      n = applyStateChange(n, ev);
      break;
    case 'run.output':
    case 'run.tool_use': {
      if (!ev.task_id) break;
      const line = logLineFromEvent(ev);
      if (line) {
        n.logs = { ...n.logs, [ev.task_id]: pushCapped(n.logs[ev.task_id], line, MAX_LOG_LINES) };
        const t = n.tasks[ev.task_id];
        if (t) n.tasks = { ...n.tasks, [ev.task_id]: { ...t, last_activity_at: ev.ts } };
        if (ev.type === 'run.tool_use' && t) {
          n.tasks = { ...n.tasks, [ev.task_id]: { ...n.tasks[ev.task_id], current_operation: line.tool ?? null } };
        }
      }
      break;
    }
    case 'run.started':
    case 'run.finished':
    case 'run.usage': {
      const t = ev.task_id ? n.tasks[ev.task_id] : null;
      if (t && ev.task_id) {
        const started = p as Partial<RunStartedPayload>;
        n.tasks = {
          ...n.tasks,
          [ev.task_id]: {
            ...t,
            ...(ev.type === 'run.started' && str(started.provider) ? { provider: str(started.provider) } : {}),
            ...(ev.type === 'run.started' ? { model: str(started.model) } : {}),
            ...(ev.type === 'run.started' && ev.agent_id ? { agent_id: ev.agent_id } : {}),
            ...(ev.type === 'run.finished' ? { current_operation: null } : {}),
            last_activity_at: ev.ts,
          },
        };
      }
      if (ev.type === 'run.usage') n.rev = { ...n.rev, usage: n.rev.usage + 1 };
      break;
    }
    case 'verification.result': {
      const st = str(p.status) as VerificationStatus | null;
      if (ev.task_id && st) n.verification = { ...n.verification, [ev.task_id]: st };
      break;
    }
    case 'agent.activity_changed': {
      const id = ev.agent_id;
      if (!id) break;
      const a = n.agents[id];
      const activity = str(p.to) as AgentActivity | null;
      if (!a || !activity) {
        n.stale = { ...n.stale, agents: addStale(n.stale.agents, id) };
        break;
      }
      const idle = activity === 'idle' || activity === 'offline';
      const working = WORKING.includes(activity);
      n.agents = {
        ...n.agents,
        [id]: {
          ...a,
          activity,
          since: ev.ts,
          current_task_id: idle ? null : (ev.task_id ?? a.current_task_id),
          current_run_id: working ? (ev.run_id ?? null) : null,
          current_operation: working ? str(p.operation) : null,
          provider: idle ? null : working ? str(p.provider) : a.provider,
          model: idle ? null : working ? str(p.model) : a.model,
        },
      };
      // An idle seat may keep its task reserved server-side (keep_task): take the exact row.
      if (idle) n.stale = { ...n.stale, agents: addStale(n.stale.agents, id) };
      break;
    }
    case 'approval.requested': {
      // payload {approval, auto_approved}
      const a = isObj(p.approval) && str(p.approval.id) ? (p.approval as unknown as Approval) : null;
      if (a) n.approvals = { ...n.approvals, [a.id]: a };
      else n.stale = { ...n.stale, approvals: addStale(n.stale.approvals, '*') };
      n.rev = { ...n.rev, approvals: n.rev.approvals + 1 };
      break;
    }
    case 'approval.resolved': {
      const r = p as Partial<ApprovalResolvedPayload>;
      const id = str(r.approval_id);
      if (!id) break;
      const a = n.approvals[id];
      if (a && typeof r.approved === 'boolean') {
        n.approvals = {
          ...n.approvals,
          [id]: { ...a, status: r.approved ? 'approved' : 'denied', resolved_at: ev.ts, resolved_by: str(r.by) ?? a.resolved_by, note: str(r.note) ?? a.note },
        };
      } else n.stale = { ...n.stale, approvals: addStale(n.stale.approvals, id) };
      n.rev = { ...n.rev, approvals: n.rev.approvals + 1 };
      break;
    }
    case 'leader.changed': {
      // Real payload (leader/switch.py): {from, to, status: switched|rolled_back, ...}. After a rollback the leader is `from`.
      const target = str(p.status) === 'rolled_back' ? p.from : p.to;
      const provider = (isObj(target) ? str(target.provider) : null) ?? str(p.provider) ?? (isObj(p.leader) ? str(p.leader.provider) : null);
      const model = (isObj(target) ? str(target.model) : null) ?? str(p.model) ?? (isObj(p.leader) ? str(p.leader.model) : null);
      if (provider) {
        n.leader = { provider, model };
        n.leaderState = 'set';
      }
      n.rev = { ...n.rev, leadership: n.rev.leadership + 1 };
      break;
    }
    case 'usage.updated':
      n.rev = { ...n.rev, usage: n.rev.usage + 1 };
      break;
    case 'provider.status':
      n.rev = { ...n.rev, providers: n.rev.providers + 1 };
      break;
    case 'router.decision':
      n.rev = { ...n.rev, routing: n.rev.routing + 1 };
      break;
    case 'skill.state_changed':
      n.rev = { ...n.rev, skills: n.rev.skills + 1 };
      break;
    case 'knowledge.indexed':
      n.rev = { ...n.rev, knowledge: n.rev.knowledge + 1 };
      break;
    case 'advisor.consulted':
    case 'decision.recorded':
      n.rev = { ...n.rev, decisions: n.rev.decisions + 1 };
      break;
    case 'system.notice':
    case 'system.recovery':
      n.notices = pushCapped(
        n.notices,
        { seq: ev.seq, ts: ev.ts, type: ev.type, message: str(p.message) ?? str(p.text) ?? ev.type, level: str(p.level) ?? 'info' },
        200,
      );
      break;
    default:
      break;
  }
  return n;
}

export function applyEvents(s: StoreData, events: ApiEvent[]): StoreData {
  return events.reduce(applyEvent, s);
}

// ---- derived helpers (pure) ----

export function activeSeconds(clock: TaskClock | undefined, task: Task | undefined, nowMs: number): number {
  const c = clock ?? { seconds: task?.active_seconds ?? 0, since: null, exact: false };
  return c.seconds + (c.since !== null ? Math.max(0, (nowMs - c.since) / 1000) : 0);
}

export function pendingApprovals(s: StoreData): Approval[] {
  return Object.values(s.approvals)
    .filter((a) => a.status === 'pending')
    .sort((a, b) => a.requested_at.localeCompare(b.requested_at));
}

export function runningTaskCount(s: StoreData): number {
  return Object.values(s.tasks).filter((t) => isActiveState(t.state)).length;
}
