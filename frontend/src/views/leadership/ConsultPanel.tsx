import { AlertTriangle, Link2, ShieldCheck } from 'lucide-react';
import { useState, type FormEvent } from 'react';
import { ApiError } from '../../api/client';
import { api } from '../../api/endpoints';
import type { ConsultResponse } from '../../api/types';
import { ListEditor } from '../../components/ListEditor';
import { ErrorBox, Field, Section } from '../../components/ui';
import { useStore } from '../../store/store';

const REFUSAL_TEXT: Record<string, string> = {
  disabled: 'The advisor policy is "never".',
  trigger_not_enabled: 'This kind of consult is not enabled by the advisor policy.',
  budget_per_task: 'The per-task consultation budget is used up.',
  budget_per_day: 'The daily consultation budget is used up.',
  no_candidate: 'No eligible advisor model is available right now.',
  call_failed: 'The advisor call failed.',
  invalid_output: 'The advisor did not return a valid structured answer.',
};

export function ConsultPanel({ onConsulted }: { onConsulted: () => void }) {
  const tasks = useStore((s) => s.data.tasks);
  const [taskId, setTaskId] = useState('');
  const [problem, setProblem] = useState('');
  const [options, setOptions] = useState<string[]>([]);
  const [constraints, setConstraints] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [res, setRes] = useState<ConsultResponse | null>(null);
  const taskList = Object.values(tasks).sort((a, b) => b.created_at.localeCompare(a.created_at));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setRes(null);
    try {
      setRes(await api.consultAdvisor({ task_id: taskId || null, problem: problem.trim(), options, constraints }));
      onConsulted();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const reason = error instanceof ApiError && (error.code === 'advisor_refused' || error.code === 'advisor_budget_exhausted') ? String(error.details.reason ?? '') : '';

  return (
    <Section title="Ask the advisor" aria="Ask the advisor">
      <form onSubmit={submit} className="grid gap-3 md:grid-cols-2">
        <div className="flex flex-col gap-3">
          <Field label="Task (optional)" htmlFor="consult-task" hint="Attaching a task gives the advisor its requirements and evidence.">
            <select id="consult-task" className="field" value={taskId} onChange={(e) => setTaskId(e.target.value)}>
              <option value="">No task</option>
              {taskList.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.title} ({t.state})
                </option>
              ))}
            </select>
          </Field>
          <Field label="Problem" htmlFor="consult-problem">
            <textarea id="consult-problem" className="field min-h-24" value={problem} maxLength={20000} onChange={(e) => setProblem(e.target.value)} placeholder="What decision do you need a second opinion on?" />
          </Field>
        </div>
        <div className="flex flex-col gap-3">
          <ListEditor label="Options considered" items={options} onChange={(x) => setOptions(x.slice(0, 8))} placeholder="Add an option (max 8)" />
          <ListEditor label="Constraints" items={constraints} onChange={(x) => setConstraints(x.slice(0, 8))} placeholder="Add a constraint (max 8)" />
        </div>
        <div className="md:col-span-2">
          <button className="btn btn-primary" disabled={busy || !problem.trim()}>
            {busy ? 'Asking...' : 'Ask advisor'}
          </button>
          <span className="ml-3 text-[11px] text-faint">One consult is one model call and counts against the consultation budgets.</span>
        </div>
      </form>
      {error ? (
        <div className="flex flex-col gap-1">
          <ErrorBox error={error} />
          {reason ? (
            <span className="text-xs text-muted" data-testid="refusal-reason">
              Why: {REFUSAL_TEXT[reason] ?? reason}
            </span>
          ) : null}
        </div>
      ) : null}
      {res && <ConsultAnswer r={res} />}
    </Section>
  );
}

export function ConsultAnswer({ r }: { r: ConsultResponse }) {
  const a = r.result;
  const independent = r.independence === 'different_provider';
  return (
    <div className="flex flex-col gap-3 border-t border-line pt-3" data-testid="consult-answer" aria-label="Advisor answer">
      <div
        className={`flex items-start gap-2 rounded-pixel border px-3 py-2 text-xs ${independent ? 'border-ok/50 bg-ok/10 text-ok' : 'border-amber/60 bg-amber/10 text-amber'}`}
        data-testid="independence"
        data-independence={r.independence}
      >
        {independent ? <ShieldCheck className="mt-0.5 size-4 shrink-0" aria-hidden /> : <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />}
        <span>
          {independent
            ? `Independent opinion: ${r.provider} / ${r.model ?? 'default'} is a different provider than the current leader.`
            : `Less independent: ${r.provider} / ${r.model ?? 'default'} is the same provider as the current leader, so it may share its blind spots.`}
        </span>
      </div>
      <div>
        <div className="label">Recommended option</div>
        <div className="text-sm font-medium" data-testid="recommended">
          {a.recommended_option}
        </div>
        <div className="mt-1 text-xs text-muted">
          Confidence (self-reported by the model, not measured): <span className="tick-num">{Math.round(a.confidence * 100)}%</span>
        </div>
      </div>
      <div>
        <div className="label">Assessment</div>
        <p className="whitespace-pre-wrap text-[13px]">{a.assessment}</p>
      </div>
      <div className="grid gap-3 md:grid-cols-3">
        <AnswerList title="Risks" items={a.risks} />
        <AnswerList title="Disagreements" items={a.disagreements} />
        <AnswerList title="Evidence that would change this" items={a.evidence_requested} />
      </div>
      <div className="flex flex-wrap items-center gap-3 text-xs text-muted">
        {r.evidence_status !== 'verified' && (
          <span className="chip text-amber" title="No verification, commit or diff is cited, so this opinion is not backed by objective evidence">
            no objective evidence
          </span>
        )}
        <span className="inline-flex items-center gap-1">
          <Link2 className="size-3" aria-hidden /> recorded as decision <span className="font-mono">{r.decision_id}</span>
        </span>
      </div>
    </div>
  );
}

function AnswerList({ title, items }: { title: string; items: string[] }) {
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
        <span className="text-xs text-faint">none</span>
      )}
    </div>
  );
}
