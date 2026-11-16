import { useMemo } from 'react';
import { api } from '../api/endpoints';
import type { ApiEvent } from '../api/types';
import { formatTime, parseTs, useNow } from '../lib/time';
import { useResource } from '../lib/useResource';
import { useStore } from '../store/store';
import { ActivityBadge, Empty, ErrorBox, MeasuredValue, Spinner, StateBadge, Unavailable } from './ui';
import type { TaskState } from '../api/types';

function Line({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[8rem_1fr] gap-2 border-b border-line/60 py-1 text-[13px]">
      <dt className="label pt-0.5">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  );
}

const unknown = <span className="text-faint">unknown</span>;

function describe(ev: ApiEvent): string {
  const p = ev.payload ?? {};
  const bits = [p.activity, p.operation, p.tool, p.to_state ?? p.to, p.status, p.text]
    .filter((v): v is string => typeof v === 'string' && v.length > 0)
    .map((s) => (s.length > 80 ? `${s.slice(0, 80)}…` : s));
  return bits.join(' · ');
}

/**
 * Inspector for one office agent: GET /agents/{id} snapshot, overlaid with the
 * live store (activity, operation, recent events) so it updates without polling.
 */
export function AgentInspector({ agentId }: { agentId: string }) {
  const res = useResource(() => api.agent(agentId), [agentId]);
  const live = useStore((s) => s.data.agents[agentId]);
  const liveEvents = useStore((s) => s.data.agentEvents[agentId]);
  const tasks = useStore((s) => s.data.tasks);
  const now = useNow();

  const base = res.data;
  const activity = live?.activity ?? base?.activity;
  const since = live?.since ?? base?.since ?? null;
  const taskId = live?.current_task_id ?? base?.task_id ?? null;
  const liveTask = taskId ? tasks[taskId] : undefined;

  const events = useMemo(() => {
    const m = new Map<number, ApiEvent>();
    for (const e of base?.recent_events ?? []) m.set(e.seq, e);
    for (const e of liveEvents ?? []) m.set(e.seq, e);
    return [...m.values()].sort((a, b) => b.seq - a.seq).slice(0, 15);
  }, [base, liveEvents]);

  if (res.unavailable && !live) return <Unavailable what="Agent inspector" />;
  if (res.loading && !live && !base) return <div className="p-4"><Spinner /></div>;
  if (res.error && !live && !res.unavailable) return <div className="p-4"><ErrorBox error={res.error} /></div>;
  if (!live && !base) return <Empty title="Agent not found" />;

  const name = base?.name ?? live?.name ?? agentId;
  const role = base?.role ?? live?.role;
  const sinceMs = parseTs(since);
  const elapsed = sinceMs === null ? null : Math.max(0, (now - sinceMs) / 1000);
  const usage = base?.usage && Object.keys(base.usage).length ? base.usage : null;

  return (
    <section aria-label={`Agent ${name}`} className="p-4" data-testid="agent-inspector">
      <header className="mb-3 flex items-center justify-between gap-2">
        <div>
          <h2 className="display text-base">{name}</h2>
          <div className="font-mono text-[11px] text-faint">{agentId}</div>
        </div>
        {activity && <ActivityBadge activity={activity} />}
      </header>
      <dl>
        <Line label="Role">{role ?? unknown}</Line>
        <Line label="Status">{base?.status ?? activity ?? unknown}</Line>
        <Line label="Provider">{(live?.provider ?? base?.provider) ?? unknown}</Line>
        <Line label="Exact model">{(live?.model ?? base?.model) ? <span className="font-mono">{live?.model ?? base?.model}</span> : unknown}</Line>
        <Line label="Project">{base?.project_name ?? base?.project_id ?? unknown}</Line>
        <Line label="Task">
          {liveTask ? (
            <span className="inline-flex flex-wrap items-center gap-2">
              {liveTask.title} <StateBadge state={liveTask.state as TaskState} />
            </span>
          ) : base?.task_title ? (
            base.task_title
          ) : (
            unknown
          )}
        </Line>
        <Line label="Operation">{(live?.current_operation ?? base?.operation) ? <span className="font-mono text-phosphor">{live?.current_operation ?? base?.operation}</span> : unknown}</Line>
        <Line label="Elapsed in state">
          <MeasuredValue m={{ value: elapsed, exactness: elapsed === null ? 'unknown' : 'measured', source: 'since timestamp, ticking locally' }} format={(n) => new Date(n * 1000).toISOString().slice(11, 19)} />
        </Line>
        <Line label="Changed files">
          {base?.changed_files?.length ? (
            <ul className="font-mono text-xs">
              {base.changed_files.map((f) => (
                <li key={f}>{f}</li>
              ))}
            </ul>
          ) : (
            unknown
          )}
        </Line>
        <Line label="Test results">
          {base?.test_results ? <pre className="max-h-40 overflow-auto whitespace-pre-wrap font-mono text-[11px]">{JSON.stringify(base.test_results, null, 2)}</pre> : unknown}
        </Line>
        <Line label="Usage">
          {usage ? <code className="text-xs">{JSON.stringify(usage)}</code> : <span className="text-faint">unknown (not measured)</span>}
        </Line>
      </dl>
      <h3 className="label mb-1 mt-4">Recent events</h3>
      {events.length === 0 ? (
        <p className="text-xs text-faint">No events recorded for this agent yet.</p>
      ) : (
        <ul className="font-mono text-[11px]" aria-label="Recent agent events">
          {events.map((e) => (
            <li key={e.seq} className="flex gap-2 border-b border-line/40 py-0.5">
              <span className="text-faint">{formatTime(e.ts)}</span>
              <span className="text-signal">{e.type}</span>
              <span className="truncate text-muted">{describe(e)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
