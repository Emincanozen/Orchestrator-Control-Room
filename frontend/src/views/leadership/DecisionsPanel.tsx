import { FileDown } from 'lucide-react';
import { useState } from 'react';
import { api } from '../../api/endpoints';
import type { Decision, EvidenceRef } from '../../api/types';
import { ConfirmDialog, Empty, Section } from '../../components/ui';
import { formatDateTime } from '../../lib/time';
import { useStore } from '../../store/store';

const KIND_LABEL: Record<string, string> = { advisor_consult: 'advisor consult', leader_decision: 'leader decision' };

export function EvidenceFlag({ d }: { d: Decision }) {
  return d.evidence_status === 'verified' ? (
    <span className="chip text-ok" data-testid="evidence-status" data-status="verified">
      evidence cited
    </span>
  ) : (
    <span className="chip text-amber" data-testid="evidence-status" data-status="none" title="No verification, commit or diff is cited. Run ids alone are provenance, not evidence.">
      no objective evidence
    </span>
  );
}

function optionText(o: unknown): string {
  return typeof o === 'string' ? o : JSON.stringify(o);
}

function EvidenceLinks({ d }: { d: Decision }) {
  if (!d.evidence.length) {
    return (
      <p className="text-xs text-amber" data-testid="no-evidence-note">
        This decision cites no evidence at all. Treat it as an opinion and challenge it.
      </p>
    );
  }
  return (
    <ul className="flex flex-col gap-1 text-[13px]" aria-label="Evidence references">
      {d.evidence.map((e: EvidenceRef, i) => {
        const id = typeof e.id === 'string' ? e.id : null;
        const taskId = e.type === 'task' ? id : typeof e.task_id === 'string' ? e.task_id : d.task_id;
        return (
          <li key={i} className="flex flex-wrap items-center gap-2" data-testid="evidence-ref">
            <span className="chip text-signal">{e.type}</span>
            {id && <span className="font-mono text-xs">{id}</span>}
            {taskId && (
              <a className="text-xs text-signal underline" href={`#/mission?task=${encodeURIComponent(taskId)}`}>
                {e.type === 'verification' ? 'open the task verification' : e.type === 'run' ? 'open the task runs' : 'open task'}
              </a>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function DecisionDetail({ d, onExported }: { d: Decision; onExported: () => void }) {
  const projects = useStore((s) => s.data.projects);
  const project = d.project_id ? projects[d.project_id] : undefined;
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [path, setPath] = useState<string | null>(null);
  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api.exportDecision(d.id);
      setPath(r.path);
      onExported();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex flex-col gap-3" data-testid="decision-detail">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="display text-sm">{d.title}</h3>
        <span className="chip text-muted">{KIND_LABEL[d.kind] ?? d.kind}</span>
        <EvidenceFlag d={d} />
      </div>
      <div className="text-xs text-muted">
        {formatDateTime(d.created_at)}
        {project ? ` · ${project.name}` : ''}
        {d.task_id ? (
          <>
            {' · '}
            <a className="text-signal underline" href={`#/mission?task=${encodeURIComponent(d.task_id)}`}>
              task {d.task_id}
            </a>
          </>
        ) : null}
      </div>
      <Block title="Context">{d.context || <span className="text-faint">none recorded</span>}</Block>
      <div>
        <div className="label mb-1">Options</div>
        {d.options.length ? (
          <ul className="list-disc space-y-0.5 pl-4 text-[13px]">
            {d.options.map((o, i) => (
              <li key={i}>{optionText(o)}</li>
            ))}
          </ul>
        ) : (
          <span className="text-xs text-faint">none recorded</span>
        )}
      </div>
      <Block title="Decision">
        <b>{d.decision}</b>
      </Block>
      <Block title="Rationale">{d.rationale || <span className="text-faint">none recorded</span>}</Block>
      <div>
        <div className="label mb-1">Evidence</div>
        <EvidenceLinks d={d} />
      </div>
      <div>
        <button className="btn" onClick={() => { setPath(null); setError(null); setConfirm(true); }} disabled={!d.project_id} title={d.project_id ? undefined : 'This decision has no project to export into'}>
          <FileDown className="size-3.5" aria-hidden /> Export to project DECISIONS.md
        </button>
      </div>
      {confirm && (
        <ConfirmDialog
          title="Export decision"
          confirmLabel="Append to DECISIONS.md"
          busy={busy}
          error={error}
          hideActions={!!path}
          onConfirm={run}
          onCancel={() => setConfirm(false)}
          body={
            path ? (
              <p className="text-sm" role="status" data-testid="export-result">
                Appended to <span className="font-mono text-xs">{path}</span>
              </p>
            ) : (
              <p className="text-sm">
                This appends an ADR-style entry to <span className="font-mono text-xs">docs/DECISIONS.md</span> inside the project
                {project ? ` "${project.name}"` : ''}. It changes a file in your repository and is only done on this explicit request.
              </p>
            )
          }
        />
      )}
    </div>
  );
}

function Block({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="label mb-1">{title}</div>
      <p className="whitespace-pre-wrap text-[13px]">{children}</p>
    </div>
  );
}

export function DecisionsPanel({ decisions, onChanged }: { decisions: Decision[]; onChanged: () => void }) {
  const [sel, setSel] = useState<string | null>(null);
  const current = decisions.find((d) => d.id === sel) ?? null;
  return (
    <Section title="Decision records" aria="Decision records">
      {decisions.length === 0 ? (
        <Empty title="No decisions recorded yet" hint="Advisor consults and leader decisions appear here with their evidence." />
      ) : (
        <div className="grid gap-4 md:grid-cols-[minmax(0,20rem)_1fr]">
          <ul className="flex max-h-[28rem] flex-col overflow-auto border border-line" aria-label="Decisions">
            {decisions.map((d) => (
              <li key={d.id} className="border-b border-line/60 last:border-b-0">
                <button
                  className={`flex w-full flex-col gap-1 px-3 py-2 text-left text-[13px] hover:bg-raised ${d.id === sel ? 'bg-raised' : ''}`}
                  onClick={() => setSel(d.id)}
                  aria-pressed={d.id === sel}
                  data-testid="decision-row"
                >
                  <span className="truncate">{d.title}</span>
                  <span className="flex items-center gap-2 text-[11px] text-faint">
                    {formatDateTime(d.created_at)}
                    <EvidenceFlag d={d} />
                  </span>
                </button>
              </li>
            ))}
          </ul>
          <div>{current ? <DecisionDetail key={current.id} d={current} onExported={onChanged} /> : <span className="text-xs text-faint">Select a decision to see its context, options, rationale and evidence.</span>}</div>
        </div>
      )}
    </Section>
  );
}
