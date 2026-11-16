import { Target } from 'lucide-react';
import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { api } from '../../api/endpoints';
import type { Objective, ObjectiveTask, TaskState } from '../../api/types';
import { ErrorBox, Field, Modal, StateBadge } from '../../components/ui';
import { VerifyChecks } from '../../components/VerifyChecks';
import { STATE_COLOR } from '../../design/tokens';
import { formatDateTime } from '../../lib/time';
import { useStore } from '../../store/store';

export const OBJECTIVE_POLL_MS = 2000;

const STATUS_COLOR: Record<string, string> = {
  planning: 'var(--color-st-planning)',
  planned: 'var(--color-signal)',
  running: 'var(--color-st-running)',
  completed: 'var(--color-ok)',
  failed: 'var(--color-danger)',
};

export function ObjectiveStatusChip({ status }: { status: string }) {
  return (
    <span className="chip" style={{ color: STATUS_COLOR[status] ?? 'var(--color-muted)' }} data-testid="objective-status" data-status={status}>
      {status}
    </span>
  );
}

// ---------------------------------------------------------------------------------------------- graph layout

export interface GraphNode {
  task: ObjectiveTask;
  layer: number;
  row: number;
}

/** Longest-path layering. Cycles cannot occur (the backend rejects them) but are guarded against anyway. */
export function layoutGraph(tasks: ObjectiveTask[]): { nodes: GraphNode[]; layers: number } {
  const byId = new Map(tasks.map((t) => [t.id, t]));
  const depth = new Map<string, number>();
  const visiting = new Set<string>();
  const dep = (id: string): number => {
    const known = depth.get(id);
    if (known !== undefined) return known;
    if (visiting.has(id)) return 0;
    visiting.add(id);
    const t = byId.get(id);
    const d = t ? Math.max(-1, ...t.depends_on.filter((x) => byId.has(x)).map(dep)) + 1 : 0;
    visiting.delete(id);
    depth.set(id, d);
    return d;
  };
  const rows = new Map<number, number>();
  const nodes = tasks.map((task) => {
    const layer = dep(task.id);
    const row = rows.get(layer) ?? 0;
    rows.set(layer, row + 1);
    return { task, layer, row };
  });
  return { nodes, layers: Math.max(0, ...nodes.map((n) => n.layer)) + 1 };
}

const NODE_W = 168;
const NODE_H = 44;
const GAP_X = 52;
const GAP_Y = 14;

export function TaskGraph({ tasks, onOpenTask }: { tasks: ObjectiveTask[]; onOpenTask: (id: string) => void }) {
  const { nodes, layers } = useMemo(() => layoutGraph(tasks), [tasks]);
  const pos = new Map(nodes.map((n) => [n.task.id, { x: n.layer * (NODE_W + GAP_X), y: n.row * (NODE_H + GAP_Y) }]));
  const maxRows = Math.max(1, ...nodes.map((n) => n.row + 1));
  const width = layers * NODE_W + (layers - 1) * GAP_X;
  const height = maxRows * NODE_H + (maxRows - 1) * GAP_Y;
  const edges = tasks.flatMap((t) => t.depends_on.filter((d) => pos.has(d)).map((d) => ({ from: d, to: t.id })));
  return (
    <div className="overflow-auto border border-line bg-sunken p-3">
      <svg
        role="img"
        aria-label={`Task graph: ${tasks.length} tasks in ${layers} dependency layers. The task list below has the same information as text.`}
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        data-testid="task-graph"
      >
        <defs>
          <marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
            <path d="M0,0 L8,4 L0,8 z" fill="var(--color-faint)" />
          </marker>
        </defs>
        {edges.map((e) => {
          const a = pos.get(e.from)!;
          const b = pos.get(e.to)!;
          const x1 = a.x + NODE_W;
          const y1 = a.y + NODE_H / 2;
          const x2 = b.x;
          const y2 = b.y + NODE_H / 2;
          const mx = (x1 + x2) / 2;
          return <path key={`${e.from}-${e.to}`} d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`} fill="none" stroke="var(--color-line-strong)" strokeWidth={1.5} markerEnd="url(#arrow)" data-edge={`${e.from}>${e.to}`} />;
        })}
        {nodes.map((n) => {
          const p = pos.get(n.task.id)!;
          const color = STATE_COLOR[n.task.state] ?? 'var(--color-muted)';
          return (
            <g key={n.task.id} transform={`translate(${p.x},${p.y})`} tabIndex={0} role="button" aria-label={`Open task ${n.task.title}, ${n.task.state}`} style={{ cursor: 'pointer' }} data-node={n.task.id} onClick={() => onOpenTask(n.task.id)} onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && onOpenTask(n.task.id)}>
              <rect width={NODE_W} height={NODE_H} rx={2} fill="var(--color-panel)" stroke={color} strokeWidth={1.5} />
              <rect width={4} height={NODE_H} fill={color} />
              <text x={12} y={18} fontSize={11} fill="var(--color-ink)">
                {clip(n.task.title, 22)}
              </text>
              <text x={12} y={34} fontSize={9.5} fill="var(--color-muted)" fontFamily="var(--font-mono)">
                {n.task.state}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

const clip = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

/** Text alternative to the graph: tasks in dependency order, indented by layer. */
function TaskList({ tasks, onOpenTask }: { tasks: ObjectiveTask[]; onOpenTask: (id: string) => void }) {
  const { nodes } = useMemo(() => layoutGraph(tasks), [tasks]);
  const sorted = [...nodes].sort((a, b) => a.layer - b.layer || a.row - b.row);
  const title = new Map(tasks.map((t) => [t.id, t.title]));
  return (
    <ol className="flex flex-col gap-1 text-[13px]" aria-label="Task list">
      {sorted.map((n) => (
        <li key={n.task.id} style={{ paddingLeft: n.layer * 18 }} className="flex flex-wrap items-center gap-2" data-testid="objective-task" data-layer={n.layer}>
          <button className="text-left text-signal underline" onClick={() => onOpenTask(n.task.id)}>
            {n.task.title}
          </button>
          <StateBadge state={n.task.state} />
          {n.task.depends_on.length > 0 && <span className="text-[11px] text-faint">after: {n.task.depends_on.map((d) => title.get(d) ?? d).join(', ')}</span>}
          {(n.task.verify_commands?.length ?? 0) > 0 && (
            <details className="w-full text-xs text-muted" data-testid="objective-task-verify">
              <summary className="cursor-pointer">{n.task.verify_commands!.length} verify check(s)</summary>
              <VerifyChecks checks={n.task.verify_commands!} />
            </details>
          )}
        </li>
      ))}
    </ol>
  );
}

// ---------------------------------------------------------------------------------------------- detail

function Bullets({ title, items, empty }: { title: string; items: string[]; empty: string }) {
  return (
    <div>
      <div className="label mb-1">{title}</div>
      {items.length ? (
        <ul className="list-disc space-y-0.5 pl-4 text-[13px]">
          {items.map((x, i) => (
            <li key={i}>{x}</li>
          ))}
        </ul>
      ) : (
        <span className="text-xs text-faint">{empty}</span>
      )}
    </div>
  );
}

/** Polls the objective while the leader is still planning (or the plan has not finished running). */
export function useObjective(id: string, pollMs = OBJECTIVE_POLL_MS): { obj: Objective | null; error: unknown } {
  const [obj, setObj] = useState<Objective | null>(null);
  const [error, setError] = useState<unknown>(null);
  const taskStates = useStore((s) => s.data.tasks);
  const known = obj ? obj.tasks.map((t) => taskStates[t.id]?.state).join(',') : '';
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      try {
        const o = await api.objective(id);
        if (cancelled) return;
        setObj(o);
        setError(null);
        if (o.status === 'planning') timer = setTimeout(load, pollMs);
      } catch (e) {
        if (!cancelled) setError(e);
      }
    };
    void load();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [id, pollMs, known]);
  return { obj, error };
}

export function ObjectiveDetail({ objectiveId, onClose, onOpenTask, pollMs }: { objectiveId: string; onClose: () => void; onOpenTask: (taskId: string) => void; pollMs?: number }) {
  const { obj, error } = useObjective(objectiveId, pollMs);
  const live = useStore((s) => s.data.tasks);
  const projects = useStore((s) => s.data.projects);
  // task states come live from the event-sourced store when known
  const tasks: ObjectiveTask[] = (obj?.tasks ?? []).map((t) => ({ ...t, state: (live[t.id]?.state ?? t.state) as TaskState }));
  return (
    <Modal open onOpenChange={(o) => !o && onClose()} title="Objective" description={obj ? (projects[obj.project_id]?.name ?? obj.project_id) : undefined} side="right">
      <div className="flex flex-col gap-4 p-4" aria-label="Objective details">
        {error ? <ErrorBox error={error} /> : null}
        {!obj && !error && <div role="status" className="text-muted">Loading objective...</div>}
        {obj && (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <ObjectiveStatusChip status={obj.status} />
              <span className="text-xs text-faint">created {formatDateTime(obj.created_at)}</span>
              <span className="text-xs text-faint">status is derived from the real task states</span>
            </div>
            <p className="whitespace-pre-wrap text-sm" data-testid="objective-text">
              {obj.text}
            </p>
            {obj.status === 'planning' && (
              <div role="status" className="flex items-center gap-2 text-st-planning" data-testid="objective-planning">
                <span className="pulse-dot inline-block size-2 bg-st-planning" aria-hidden />
                The leader is decomposing this objective into tasks...
              </div>
            )}
            {obj.status === 'failed' && obj.tasks.length === 0 && (
              <div role="alert" className="rounded-pixel border border-danger/60 bg-danger/10 p-3 text-sm text-danger" data-testid="objective-failed">
                Planning failed: {obj.error ?? 'the backend gave no reason'}
              </div>
            )}
            {obj.status === 'failed' && obj.tasks.length > 0 && (
              <div role="alert" className="rounded-pixel border border-danger/60 bg-danger/10 p-3 text-sm text-danger">
                At least one task failed, was blocked or was cancelled.
              </div>
            )}
            {Object.keys(obj.unsafe_verify_commands).length > 0 && (
              <div role="alert" className="rounded-pixel border border-amber/60 bg-amber/10 p-3 text-xs text-amber" data-testid="unsafe-verify">
                <div className="font-medium">Proposed by the leader but not adopted: these tasks have no automatic verification yet.</div>
                <ul className="mt-1 space-y-1 text-muted">
                  {Object.entries(obj.unsafe_verify_commands).map(([key, items]) => (
                    <li key={key}>
                      <span className="font-mono text-ink">{obj.tasks.find((t) => t.key === key)?.title ?? key}</span>
                      <ul className="ml-4 list-disc">
                        {items.map((c, i) => (
                          <li key={i}>
                            <span className="font-mono">{c.command}</span> ({c.reason})
                          </li>
                        ))}
                      </ul>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {obj.status !== 'planning' && !(obj.status === 'failed' && obj.tasks.length === 0) && (
              <>
                <div className="grid gap-3 md:grid-cols-3">
                  <Bullets title="Deliverables" items={obj.deliverables} empty="none listed" />
                  <Bullets title="Prerequisites" items={obj.prerequisites} empty="none listed" />
                  <Bullets title="Acceptance criteria" items={obj.acceptance_criteria} empty="none listed" />
                </div>
                <div>
                  <div className="label mb-1">Task graph ({tasks.length} tasks)</div>
                  {tasks.length ? (
                    <div className="flex flex-col gap-3">
                      <TaskGraph tasks={tasks} onOpenTask={onOpenTask} />
                      <TaskList tasks={tasks} onOpenTask={onOpenTask} />
                    </div>
                  ) : (
                    <span className="text-xs text-faint">No tasks were created.</span>
                  )}
                </div>
                {obj.status === 'planned' && <p className="text-xs text-muted">Tasks were created but not started. Start them from the board; dependencies gate execution.</p>}
              </>
            )}
          </>
        )}
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------------------------------------- form + list

export function ObjectiveForm({ defaultProjectId, onClose, onCreated }: { defaultProjectId: string | null; onClose: () => void; onCreated: (o: Objective) => void }) {
  const projects = useStore((s) => s.data.projects);
  const list = Object.values(projects).sort((a, b) => a.name.localeCompare(b.name));
  const [projectId, setProjectId] = useState(defaultProjectId ?? list[0]?.id ?? '');
  const [text, setText] = useState('');
  const [autostart, setAutostart] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const o = await api.createObjective(projectId, { text: text.trim(), autostart });
      onCreated(o);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal open onOpenChange={(o) => !o && onClose()} title="New objective" description="The leader decomposes the goal into dependency-aware tasks">
      <form onSubmit={submit} className="flex flex-col gap-3 p-4">
        <Field label="Project" htmlFor="obj-project">
          <select id="obj-project" className="field" value={projectId} onChange={(e) => setProjectId(e.target.value)}>
            {list.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Objective" htmlFor="obj-text" hint="Describe the outcome you want, not the steps. Max 20,000 characters.">
          <textarea id="obj-text" className="field min-h-28" value={text} maxLength={20000} onChange={(e) => setText(e.target.value)} placeholder="e.g. Add CSV export to the reports page, with tests" />
        </Field>
        <label className="flex items-start gap-2 text-sm">
          <input type="checkbox" className="mt-1" checked={autostart} onChange={(e) => setAutostart(e.target.checked)} />
          <span>
            Start the tasks as soon as they are planned
            <span className="block text-[11px] text-faint">Off by default: you review the plan first. Dependencies still gate execution.</span>
          </span>
        </label>
        {error ? <ErrorBox error={error} /> : null}
        <div className="flex justify-end gap-2">
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" disabled={busy || !text.trim() || !projectId}>
            {busy ? 'Submitting...' : 'Plan objective'}
          </button>
        </div>
      </form>
    </Modal>
  );
}

export function ObjectivesList({ projectId, onClose, onOpen }: { projectId: string | null; onClose: () => void; onOpen: (id: string) => void }) {
  const projects = useStore((s) => s.data.projects);
  const [rows, setRows] = useState<Objective[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    let cancelled = false;
    const ids = projectId ? [projectId] : Object.keys(projects);
    Promise.all(ids.map((id) => api.objectives(id)))
      .then((r) => !cancelled && setRows(r.flat().sort((a, b) => b.created_at.localeCompare(a.created_at))))
      .catch((e) => !cancelled && setError(e));
    return () => {
      cancelled = true;
    };
  }, [projectId, projects]);
  return (
    <Modal open onOpenChange={(o) => !o && onClose()} title="Objectives" description={projectId ? projects[projectId]?.name : 'All projects'}>
      <div className="p-4">
        {error ? <ErrorBox error={error} /> : null}
        {rows === null && !error && <div role="status" className="text-muted">Loading...</div>}
        {rows && rows.length === 0 && <div className="text-sm text-faint">No objectives yet.</div>}
        {rows && rows.length > 0 && (
          <ul className="flex flex-col gap-2" aria-label="Objectives">
            {rows.map((o) => (
              <li key={o.id}>
                <button className="pixel-card flex w-full items-center gap-3 px-3 py-2 text-left text-[13px] hover:border-phosphor" onClick={() => onOpen(o.id)}>
                  <ObjectiveStatusChip status={o.status} />
                  <span className="truncate">{o.text}</span>
                  <span className="ml-auto shrink-0 text-[11px] text-faint">{o.tasks.length} tasks</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Modal>
  );
}

export function ObjectiveButton({ onClick }: { onClick: () => void }) {
  return (
    <button className="btn" onClick={onClick}>
      <Target className="size-3.5" aria-hidden /> New objective
    </button>
  );
}
