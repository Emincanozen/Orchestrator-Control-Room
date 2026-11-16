import { create } from 'zustand';
import { api } from '../api/endpoints';
import { ApiError, setUnauthorizedHandler } from '../api/client';
import { getToken } from '../api/auth';
import type { ApiEvent } from '../api/types';
import { EventStream, type ConnStatus } from './stream';
import {
  applyEvents,
  applySnapshot,
  initialData,
  removeProject,
  resolveClock,
  seedLog,
  unresolvedClocks,
  upsertAgent,
  upsertApproval,
  upsertProject,
  upsertTask,
  type StoreData,
} from './reducer';
import type { Agent, Approval, Project, Task } from '../api/types';

interface StoreState {
  data: StoreData;
  conn: ConnStatus;
  authFailed: boolean;
  bootError: string | null;
  booting: boolean;
  /** features whose endpoints answered 404/501 during bootstrap */
  unavailable: { agents: boolean; approvals: boolean };

  bootstrap: () => Promise<void>;
  shutdown: () => void;
  ingest: (events: ApiEvent[]) => void;
  refetchStale: () => Promise<void>;
  /** Pins the running segment of active tasks (GET /tasks/{id}/transitions). */
  syncClocks: () => Promise<void>;
  /** After a WS replay: re-read tasks/approvals/agents so replayed creation snapshots cannot leave stale fields. */
  reconcile: (opts?: { prune?: boolean }) => Promise<void>;
  loadTaskLog: (taskId: string) => Promise<void>;
  // local cache updates after successful REST mutations (events will confirm)
  putTask: (t: Task) => void;
  putProject: (p: Project) => void;
  dropProject: (id: string) => void;
  putApproval: (a: Approval) => void;
  putAgent: (a: Agent) => void;
}

/** Raw event listeners (Logs view): receive every ingested batch, independent of the capped `recent` list. */
const eventListeners = new Set<(events: ApiEvent[]) => void>();
export function subscribeEvents(fn: (events: ApiEvent[]) => void): () => void {
  eventListeners.add(fn);
  return () => eventListeners.delete(fn);
}

let stream: EventStream | null = null;
const clockInFlight = new Set<string>();
let flushTimer: ReturnType<typeof setTimeout> | null = null;
let buffer: ApiEvent[] = [];

export const useStore = create<StoreState>((set, get) => ({
  data: initialData(),
  conn: 'idle',
  authFailed: false,
  bootError: null,
  booting: false,
  unavailable: { agents: false, approvals: false },

  async bootstrap() {
    if (get().booting) return;
    set({ booting: true, bootError: null });
    setUnauthorizedHandler(() => set({ authFailed: true }));
    try {
      // /status first: its last_seq is the resume point for the WS replay.
      let lastSeq = 0;
      let leader: StoreData['leader'] = null;
      let leaderState: StoreData['leaderState'] = null;
      try {
        const st = await api.status();
        lastSeq = st.last_seq;
        leader = st.leader;
        leaderState = st.leader_state ?? null;
      } catch (e) {
        if (!(e instanceof ApiError && e.unavailable)) throw e;
      }
      const [projects, tasks, agents, approvals] = await Promise.allSettled([
        api.projects(),
        api.tasks(),
        api.agents(),
        api.approvals('pending'),
      ]);
      if (projects.status === 'rejected') throw projects.reason;
      if (tasks.status === 'rejected') throw tasks.reason;
      const unavail = (r: PromiseSettledResult<unknown>) =>
        r.status === 'rejected' && r.reason instanceof ApiError && r.reason.unavailable;
      for (const r of [agents, approvals]) if (r.status === 'rejected' && !unavail(r)) throw r.reason;
      set((s) => ({
        data: applySnapshot(s.data, {
          projects: projects.value,
          tasks: tasks.value,
          agents: agents.status === 'fulfilled' ? agents.value : [],
          approvals: approvals.status === 'fulfilled' ? approvals.value : [],
          lastSeq,
          leader,
          leaderState,
        }),
        unavailable: { agents: unavail(agents), approvals: unavail(approvals) },
      }));
      void get().syncClocks();
      stream?.stop();
      let requestedSeq = get().data.lastSeq;
      stream = new EventStream({
        getAfterSeq: () => (requestedSeq = get().data.lastSeq),
        getToken,
        onEvents: (evs) => {
          buffer.push(...evs);
          if (!flushTimer) {
            flushTimer = setTimeout(() => {
              flushTimer = null;
              const b = buffer;
              buffer = [];
              get().ingest(b);
            }, 40);
          }
        },
        onStatus: (conn) => set({ conn }),
        onAuthFailure: () => set({ authFailed: true }),
        onForbidden: () => set({ bootError: "The backend rejected this page's Host/Origin (WebSocket close 4403). Open the dashboard through the address the backend serves (127.0.0.1 or localhost)." }),
        onControl: (c) => {
          // replay.done.seq is the last replayed event; beyond what we asked for means events were replayed.
          // Wait one flush interval so the replayed batch is applied before we reconcile.
          if (c.type === 'replay.done' && c.seq > requestedSeq) setTimeout(() => void get().reconcile(), 120);
          // Truncated replay: events between requested_after_seq and resume_after_seq were skipped for good, so the
          // event-sourced state may be stale. The snapshots are the source of truth: reconcile after the replay batch.
          else if (c.type === 'replay.truncated') setTimeout(() => void get().reconcile({ prune: true }), 400);
        },
      });
      stream.start();
    } catch (e) {
      if (e instanceof ApiError && e.unauthorized) set({ authFailed: true });
      else set({ bootError: e instanceof Error ? e.message : String(e) });
    } finally {
      set({ booting: false });
    }
  },

  shutdown() {
    stream?.stop();
    stream = null;
    if (flushTimer) clearTimeout(flushTimer);
    flushTimer = null;
    buffer = [];
    set({ conn: 'idle', data: initialData() });
  },

  ingest(events) {
    eventListeners.forEach((fn) => fn(events));
    set((s) => ({ data: applyEvents(s.data, events) }));
    const st = get().data.stale;
    if (st.tasks.length || st.agents.length || st.approvals.length || st.projects.length) void get().refetchStale();
    if (unresolvedClocks(get().data).length) void get().syncClocks();
  },

  async syncClocks() {
    const ids = unresolvedClocks(get().data).filter((id) => !clockInFlight.has(id));
    await Promise.allSettled(
      ids.map(async (id) => {
        clockInFlight.add(id);
        try {
          const trs = await api.taskTransitions(id);
          set((s) => ({ data: resolveClock(s.data, id, trs) }));
        } finally {
          clockInFlight.delete(id);
        }
      }),
    );
  },

  async reconcile(opts) {
    void api.status().then((st) => {
      if (st.leader_state) set((s) => ({ data: { ...s.data, leaderState: st.leader_state ?? null, leader: st.leader ?? s.data.leader } }));
    }).catch(() => undefined);
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const seqBefore = get().data.lastSeq;
      const [tasks, approvals, agents] = await Promise.allSettled([api.tasks(), api.approvals('pending'), api.agents()]);
      if (get().data.lastSeq !== seqBefore) continue; // newer events arrived meanwhile: our read may be older, retry
      set((s) => {
        let d = s.data;
        if (tasks.status === 'fulfilled') for (const t of tasks.value) d = upsertTask(d, t);
        if (approvals.status === 'fulfilled') {
          for (const a of approvals.value) d = upsertApproval(d, a);
          if (opts?.prune) {
            // Events were skipped: a pending approval that is not in the fresh pending list was resolved in the gap.
            const fresh = new Set(approvals.value.map((a) => a.id));
            const kept = Object.fromEntries(Object.entries(d.approvals).filter(([id, a]) => a.status !== 'pending' || fresh.has(id)));
            d = { ...d, approvals: kept };
          }
        }
        if (agents.status === 'fulfilled') for (const a of agents.value) d = upsertAgent(d, a);
        return { data: d };
      });
      void get().syncClocks();
      return;
    }
  },

  async refetchStale() {
    const { tasks, agents, approvals, projects } = get().data.stale;
    // Clear first: events that arrive while the GETs are in flight re-queue their ids.
    set((s) => ({ data: { ...s.data, stale: { tasks: [], agents: [], approvals: [], projects: [] } } }));
    await Promise.allSettled([
      ...tasks.map((id) => api.task(id).then((t) => get().putTask(t))),
      ...(agents.length ? [api.agents().then((as) => as.filter((a) => agents.includes(a.id)).forEach((a) => get().putAgent(a)))] : []),
      ...(approvals.length ? [api.approvals().then((as) => as.forEach((a) => get().putApproval(a)))] : []),
      ...projects.map((id) => api.project(id).then((p) => get().putProject(p))),
    ]);
    void get().syncClocks();
  },

  async loadTaskLog(taskId) {
    try {
      const events = await api.events({ task_id: taskId, limit: 2000 });
      set((s) => ({ data: seedLog(s.data, taskId, events) }));
    } catch {
      /* log backlog is best-effort; live events still stream */
    }
  },

  putTask: (t) => {
    set((s) => ({ data: upsertTask(s.data, t) }));
    void get().syncClocks();
  },
  putProject: (p) => set((s) => ({ data: upsertProject(s.data, p) })),
  dropProject: (id) => set((s) => ({ data: removeProject(s.data, id) })),
  putApproval: (a) => set((s) => ({ data: upsertApproval(s.data, a) })),
  putAgent: (a) => set((s) => ({ data: upsertAgent(s.data, a) })),
}));
