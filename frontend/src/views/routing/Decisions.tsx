import { ChevronDown, ChevronRight, Play } from 'lucide-react';
import { useEffect, useState } from 'react';
import { api } from '../../api/endpoints';
import type { PreviewRequest, RoutingDecision, RoutingPreview } from '../../api/types';
import { Async, Empty, ErrorBox, Field } from '../../components/ui';
import { formatDateTime } from '../../lib/time';
import { useResource } from '../../lib/useResource';
import { useStore } from '../../store/store';
import { Explanation, type ExplainInput } from './Explanation';

export function explainDecision(d: RoutingDecision): ExplainInput {
  return {
    chosen: { provider: d.chosen_provider, model: d.chosen_model },
    classification: d.detail?.classification,
    decision: d.detail?.decision,
    candidates: d.candidates ?? [],
    reasons: d.reasons?.length ? d.reasons : (d.detail?.reasons ?? []),
    ruleIds: d.rule_ids?.length ? d.rule_ids : (d.detail?.rule_ids ?? []),
    adaptive: d.adaptive_applied,
    fallback: d.detail?.fallback,
  };
}

export function explainPreview(p: RoutingPreview): ExplainInput {
  const d = p.decision;
  return {
    outcome: p.outcome,
    chosen: d.provider ? { provider: d.provider, model: d.model } : null,
    classification: p.classification,
    decision: d,
    candidates: d.candidates ?? [],
    reasons: d.reasons ?? [],
    ruleIds: d.rule_ids ?? [],
    adaptive: Boolean(d.adaptive_applied),
  };
}

function DecisionRow({ d, taskTitle }: { d: RoutingDecision; taskTitle: string | null }) {
  const [open, setOpen] = useState(false);
  const panelId = `decision-${d.id}`;
  return (
    <li className="pixel-card" data-testid="decision">
      <button className="flex w-full flex-wrap items-center gap-3 p-3 text-left text-[13px]" aria-expanded={open} aria-controls={panelId} onClick={() => setOpen((o) => !o)}>
        {open ? <ChevronDown className="size-4 text-muted" aria-hidden /> : <ChevronRight className="size-4 text-muted" aria-hidden />}
        <span className="chip text-signal">{d.phase}</span>
        <span className="font-mono">
          {d.chosen_provider} / {d.chosen_model ?? 'default'}
        </span>
        {d.adaptive_applied && <span className="chip text-amber">adaptive</span>}
        <span className="min-w-0 truncate text-xs text-muted">{taskTitle ?? d.task_id}</span>
        <span className="ml-auto text-xs text-faint">{formatDateTime(d.created_at)}</span>
        <span className="sr-only">Why this model?</span>
      </button>
      {open && (
        <div id={panelId}>
          <Explanation x={explainDecision(d)} />
        </div>
      )}
    </li>
  );
}

export function DecisionsList({ taskId, onTaskChange, rev, onLoaded }: { taskId: string; onTaskChange: (id: string) => void; rev: number; onLoaded?: () => void }) {
  const tasks = useStore((s) => s.data.tasks);
  const res = useResource(() => api.routingDecisions({ task_id: taskId || undefined, limit: 100 }), [taskId, rev]);
  useEffect(() => {
    if (res.data) onLoaded?.();
  }, [res.data, onLoaded]);
  const taskList = Object.values(tasks).sort((a, b) => a.title.localeCompare(b.title));
  return (
    <section aria-label="Routing decisions" className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="label">Why this model?</h2>
        <select className="field w-auto max-w-xs" aria-label="Filter decisions by task" value={taskId} onChange={(e) => onTaskChange(e.target.value)}>
          <option value="">All tasks</option>
          {taskId && !tasks[taskId] && <option value={taskId}>{taskId}</option>}
          {taskList.map((t) => (
            <option key={t.id} value={t.id}>
              {t.title}
            </option>
          ))}
        </select>
      </div>
      <Async res={res} what="Routing decisions">
        {(rows) =>
          rows.length === 0 ? (
            <Empty title="No routing decisions yet" hint="A decision is recorded each time the router picks a model for a run." />
          ) : (
            <ul className="flex flex-col gap-2" aria-label="Decisions">
              {rows.map((d) => (
                <DecisionRow key={d.id} d={d} taskTitle={tasks[d.task_id]?.title ?? null} />
              ))}
            </ul>
          )
        }
      </Async>
    </section>
  );
}

const PHASES: PreviewRequest['phase'][] = ['execute', 'plan', 'repair', 'review', 'advise'];

export function PreviewForm() {
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [taskType, setTaskType] = useState('');
  const [role, setRole] = useState('');
  const [phase, setPhase] = useState<PreviewRequest['phase']>('execute');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [result, setResult] = useState<RoutingPreview | null>(null);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      setResult(
        await api.routingPreview({
          title: title.trim(),
          description,
          task_type: taskType.trim() || null,
          role: role.trim() || null,
          phase,
        }),
      );
    } catch (e) {
      setResult(null);
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section aria-label="Routing preview" className="flex flex-col gap-3">
      <h2 className="label">Preview a routing decision</h2>
      <p className="text-xs text-muted">Shows what the router would do now. Nothing is run, recorded or queued.</p>
      <form
        className="pixel-card grid grid-cols-1 gap-3 p-3 md:grid-cols-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (title.trim()) void submit();
        }}
      >
        <div className="md:col-span-2">
          <Field label="Title" htmlFor="pv-title">
            <input id="pv-title" className="field" value={title} onChange={(e) => setTitle(e.target.value)} maxLength={500} required />
          </Field>
        </div>
        <div className="md:col-span-2">
          <Field label="Description" htmlFor="pv-desc">
            <textarea id="pv-desc" className="field min-h-16" value={description} onChange={(e) => setDescription(e.target.value)} />
          </Field>
        </div>
        <Field label="Task type (optional)" htmlFor="pv-type" hint="Leave empty to let the classifier decide.">
          <input id="pv-type" className="field" value={taskType} onChange={(e) => setTaskType(e.target.value)} />
        </Field>
        <Field label="Role (optional)" htmlFor="pv-role">
          <input id="pv-role" className="field" value={role} onChange={(e) => setRole(e.target.value)} />
        </Field>
        <Field label="Phase" htmlFor="pv-phase">
          <select id="pv-phase" className="field" value={phase} onChange={(e) => setPhase(e.target.value as PreviewRequest['phase'])}>
            {PHASES.map((p) => (
              <option key={p}>{p}</option>
            ))}
          </select>
        </Field>
        <div className="flex items-end">
          <button type="submit" className="btn btn-primary" disabled={busy || !title.trim()}>
            <Play className="size-3.5" aria-hidden /> Preview
          </button>
        </div>
      </form>
      {error ? <ErrorBox error={error} /> : null}
      {result && (
        <div className="pixel-card" aria-label="Preview result" role="region">
          <Explanation x={explainPreview(result)} />
        </div>
      )}
    </section>
  );
}
