import { useEffect, useRef, useState } from 'react';
import { ApiError } from '../../api/client';
import { api } from '../../api/endpoints';
import {
  KNOWLEDGE_SOURCES,
  type IndexReport,
  type KnowledgeDoc,
  type KnowledgeSource,
  type KnowledgeStats,
} from '../../api/types';
import { ConfirmDialog, ErrorBox, Modal } from '../../components/ui';
import { formatDateTime } from '../../lib/time';
import { useStore } from '../../store/store';

export const LEXICAL_NOTE =
  'Search is full-text (lexical, SQLite FTS5): it matches words, not meaning. Optional hash vectors may add a little recall for similar spellings; they are not semantic embeddings.';

export function ProjectSelect({ id, value, onChange, allLabel }: { id: string; value: string; onChange: (v: string) => void; allLabel: string }) {
  const projects = useStore((s) => s.data.projects);
  const list = Object.values(projects).sort((a, b) => a.name.localeCompare(b.name));
  return (
    <select id={id} className="field w-auto" aria-label="Project" value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">{allLabel}</option>
      {list.map((p) => (
        <option key={p.id} value={p.id}>
          {p.name}
        </option>
      ))}
    </select>
  );
}

export function StatsPanel({ stats }: { stats: KnowledgeStats }) {
  const projects = useStore((s) => s.data.projects);
  const v = stats.vector_search;
  return (
    <section aria-label="Knowledge statistics" className="pixel-card grid gap-4 p-4 md:grid-cols-4" data-testid="knowledge-stats">
      <Stat label="Documents" value={String(stats.docs)} />
      <Stat label="Text indexed" value={`${(stats.chars / 1000).toFixed(1)}k chars`} />
      <Stat label="Last updated" value={stats.last_updated ? formatDateTime(stats.last_updated) : 'never'} />
      <div>
        <div className="label">Vectors</div>
        <div className="text-sm">
          {v.enabled ? (
            <>
              <span className="tick-num">{stats.vectors}</span> hash vectors ({v.embedder})
            </>
          ) : (
            'off (full-text only)'
          )}
        </div>
        <div className="text-[11px] text-faint">{v.semantic ? 'embedder reports itself as semantic' : 'not semantic'}</div>
      </div>
      <div className="md:col-span-2">
        <div className="label mb-1">By kind</div>
        <ChipCounts counts={stats.by_kind} />
      </div>
      <div className="md:col-span-2">
        <div className="label mb-1">By project</div>
        <ChipCounts counts={Object.fromEntries(Object.entries(stats.by_project).map(([k, n]) => [projects[k]?.name ?? k, n]))} />
      </div>
    </section>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="label">{label}</div>
      <div className="tick-num text-sm">{value}</div>
    </div>
  );
}

function ChipCounts({ counts }: { counts: Record<string, number> }) {
  const e = Object.entries(counts);
  if (!e.length) return <span className="text-xs text-faint">nothing indexed</span>;
  return (
    <ul className="flex flex-wrap gap-1.5">
      {e.map(([k, n]) => (
        <li key={k} className="chip text-signal">
          {k} <span className="tick-num text-ink">{n}</span>
        </li>
      ))}
    </ul>
  );
}

/** Plain-text viewer: the body is rendered inside <pre> as text, never as HTML or markdown. */
export function DocViewer({ docId, onClose, onDeleted }: { docId: string; onClose: () => void; onDeleted: () => void }) {
  const [doc, setDoc] = useState<KnowledgeDoc | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [delError, setDelError] = useState<unknown>(null);
  useEffect(() => {
    let cancelled = false;
    api
      .knowledgeDoc(docId)
      .then((d) => !cancelled && setDoc(d))
      .catch((e) => !cancelled && setError(e));
    return () => {
      cancelled = true;
    };
  }, [docId]);
  const del = async () => {
    setBusy(true);
    setDelError(null);
    try {
      await api.deleteKnowledgeDoc(docId);
      onDeleted();
    } catch (e) {
      setDelError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal open onOpenChange={(o) => !o && onClose()} title={doc?.title ?? 'Document'} description={doc?.source_path ?? undefined} side="right">
      <div className="flex flex-col gap-3 p-4">
        {error ? <ErrorBox error={error} /> : null}
        {!doc && !error && <div role="status" className="text-muted">Loading...</div>}
        {doc && (
          <>
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 text-xs text-muted">
              <dt>kind</dt>
              <dd>{doc.kind}</dd>
              <dt>source</dt>
              <dd className="font-mono">{doc.source_path ?? 'none'}</dd>
              <dt>updated</dt>
              <dd>{formatDateTime(doc.updated_at)}</dd>
              <dt>hash</dt>
              <dd className="font-mono">{doc.source_hash.slice(0, 16)}</dd>
            </dl>
            <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap bg-void p-3 font-mono text-[12px]" data-testid="doc-body">
              {doc.body}
            </pre>
            <div>
              <button className="btn btn-danger" onClick={() => setConfirm(true)}>
                Delete from index
              </button>
              <span className="ml-3 text-[11px] text-faint">Removes the index entry only, never the source file. A later re-index may add it again.</span>
            </div>
          </>
        )}
        {confirm && (
          <ConfirmDialog
            title="Delete document from the index"
            confirmLabel="Delete"
            danger
            busy={busy}
            error={delError}
            onConfirm={del}
            onCancel={() => setConfirm(false)}
            body={
              <p className="text-sm">
                Delete <b>{doc?.title}</b> from the knowledge index? The source file is not touched.
              </p>
            }
          />
        )}
      </div>
    </Modal>
  );
}

const SOURCE_TEXT: Record<KnowledgeSource, string> = {
  docs: 'project docs and requirements',
  tasks: 'task history',
  errors: 'resolved errors',
  code: 'code metadata',
  skills: 'skills',
  decisions: 'decision records',
  models: 'model history',
};

export function IndexDialog({ onClose }: { onClose: () => void }) {
  const [projectId, setProjectId] = useState('');
  const [sources, setSources] = useState<KnowledgeSource[]>([...KNOWLEDGE_SOURCES]);
  const [full, setFull] = useState(false);
  const [phase, setPhase] = useState<'form' | 'running' | 'done' | 'failed'>('form');
  const [report, setReport] = useState<IndexReport | null>(null);
  const [error, setError] = useState<unknown>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);

  const poll = (jobId: string) => {
    api
      .knowledgeJob(jobId)
      .then((j) => {
        if (j.status === 'running') {
          timer.current = setTimeout(() => poll(jobId), 600);
          return;
        }
        setReport(j.report);
        setPhase(j.status === 'done' ? 'done' : 'failed');
      })
      .catch((e) => {
        setError(e);
        setPhase('failed');
      });
  };
  const start = async () => {
    setError(null);
    setPhase('running');
    try {
      const r = await api.knowledgeIndex({ project_id: projectId || null, sources: sources.length === KNOWLEDGE_SOURCES.length ? undefined : sources, full });
      poll(r.job_id);
    } catch (e) {
      setError(e);
      setPhase('failed');
    }
  };
  const toggle = (s: KnowledgeSource) => setSources((cur) => (cur.includes(s) ? cur.filter((x) => x !== s) : [...cur, s]));
  return (
    <Modal open onOpenChange={(o) => !o && onClose()} title="Index now" description="Updates the local full-text index. Nothing leaves this machine." wide>
      <div className="flex flex-col gap-4 p-4">
        {(phase === 'form' || phase === 'running') && (
          <>
            <div className="flex flex-col gap-1">
              <label htmlFor="idx-project" className="label">
                Project
              </label>
              <ProjectSelect id="idx-project" value={projectId} onChange={setProjectId} allLabel="All projects" />
            </div>
            <fieldset className="flex flex-col gap-1">
              <legend className="label mb-1">Sources</legend>
              <div className="grid gap-1 sm:grid-cols-2">
                {KNOWLEDGE_SOURCES.map((s) => (
                  <label key={s} className="flex items-center gap-2 text-[13px]">
                    <input type="checkbox" checked={sources.includes(s)} onChange={() => toggle(s)} disabled={phase === 'running'} />
                    <span className="font-mono">{s}</span>
                    <span className="text-faint">{SOURCE_TEXT[s]}</span>
                  </label>
                ))}
              </div>
            </fieldset>
            <label className="flex items-start gap-2 text-sm">
              <input type="checkbox" className="mt-1" checked={full} onChange={(e) => setFull(e.target.checked)} disabled={phase === 'running'} />
              <span>
                Full reindex
                <span className="block text-[11px] text-faint">Re-reads everything instead of only changed files. Slower.</span>
              </span>
            </label>
          </>
        )}
        {phase === 'running' && (
          <div role="status" className="text-phosphor" data-testid="index-running">
            Indexing...
          </div>
        )}
        {error ? <ErrorBox error={error instanceof ApiError ? error : error} /> : null}
        {(phase === 'done' || phase === 'failed') && report && <ReportView report={report} failed={phase === 'failed'} />}
        <div className="flex justify-end gap-2">
          {phase === 'form' && (
            <>
              <button className="btn" onClick={onClose}>
                Cancel
              </button>
              <button className="btn btn-primary" onClick={start} disabled={sources.length === 0}>
                Start indexing
              </button>
            </>
          )}
          {phase !== 'form' && phase !== 'running' && (
            <button className="btn" onClick={onClose}>
              Close
            </button>
          )}
        </div>
      </div>
    </Modal>
  );
}

export function ReportView({ report, failed }: { report: IndexReport; failed: boolean }) {
  return (
    <div className="flex flex-col gap-3" data-testid="index-report" data-failed={failed}>
      <div className={`text-sm ${failed ? 'text-danger' : 'text-ok'}`}>{failed ? 'Indexing failed' : 'Indexing finished'}</div>
      <div className="grid grid-cols-4 gap-3">
        {(['added', 'updated', 'removed', 'skipped'] as const).map((k) => (
          <div key={k} className="pixel-card p-2 text-center">
            <div className="tick-num text-lg" data-testid={`report-${k}`}>
              {report[k]}
            </div>
            <div className="label">{k}</div>
          </div>
        ))}
      </div>
      {report.skipped_details.length > 0 && (
        <div>
          <div className="label mb-1">Skipped ({report.skipped_details.length}{report.skipped_details.length >= 50 ? '+, first 50 shown' : ''})</div>
          <ul className="max-h-40 overflow-auto text-xs" aria-label="Skipped files">
            {report.skipped_details.map((s, i) => (
              <li key={i} className="flex gap-2 border-b border-line/40 py-0.5">
                <span className="truncate font-mono">{s.path}</span>
                <span className="ml-auto shrink-0 text-faint">{s.reason}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {report.errors.length > 0 && (
        <div>
          <div className="label mb-1 text-danger">Errors ({report.errors.length})</div>
          <ul className="max-h-40 overflow-auto font-mono text-xs text-danger" aria-label="Index errors">
            {report.errors.map((e, i) => (
              <li key={i}>{e}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
