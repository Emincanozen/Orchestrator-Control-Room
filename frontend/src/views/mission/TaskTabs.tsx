import { FileText, History, Save } from 'lucide-react';
import { useState } from 'react';
import { api } from '../../api/endpoints';
import { ApiError } from '../../api/client';
import type { Task } from '../../api/types';
import { Async, Empty, ErrorBox, Modal } from '../../components/ui';
import { formatDateTime } from '../../lib/time';
import { isActiveState } from '../../lib/transitions';
import { useResource } from '../../lib/useResource';
import { useStore } from '../../store/store';
import { DiffView } from './DiffView';
import { useTaskResource } from './useTaskResource';

/** 404 on workspace endpoints means "the task has no workspace yet", not "endpoint missing". */
function noWorkspace(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

export function TimelineTab({ taskId }: { taskId: string }) {
  const res = useTaskResource(taskId, () => api.taskTransitions(taskId));
  return (
    <Async res={res} what="Timeline">
      {(rows) =>
        rows.length === 0 ? (
          <Empty title="No state transitions recorded" />
        ) : (
          <ol className="px-4 py-3" aria-label="State transitions" data-testid="timeline">
            {rows.map((e) => (
              <li key={e.id} className="relative border-l border-line-strong pb-4 pl-4">
                <span aria-hidden className="absolute -left-[5px] top-1 size-2 bg-phosphor" />
                <div className="font-mono text-xs text-faint">{formatDateTime(e.ts)}</div>
                <div className="text-[13px]">
                  <span className="font-mono text-muted">{e.from_state ?? 'start'}</span> {'→'} <span className="font-mono text-ink">{e.to_state}</span>
                  <span className="ml-2 text-xs text-faint">by {e.actor}</span>
                </div>
                {e.reason ? <div className="text-xs text-muted">{e.reason}</div> : null}
              </li>
            ))}
          </ol>
        )
      }
    </Async>
  );
}

export function CheckpointsTab({ task }: { task: Task }) {
  const taskId = task.id;
  const putTask = useStore((s) => s.putTask);
  const res = useTaskResource(taskId, () => api.taskCheckpoints(taskId));
  const [label, setLabel] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [confirm, setConfirm] = useState<string | null>(null);
  const active = isActiveState(task.state);

  const afterChange = async () => {
    res.reload();
    try {
      putTask(await api.task(taskId));
    } catch {
      /* the event stream will catch up */
    }
  };
  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.createCheckpoint(taskId, label.trim() || 'manual');
      setLabel('');
      await afterChange();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  const restore = async (id: string) => {
    setBusy(true);
    setError(null);
    try {
      await api.restoreCheckpoint(taskId, id);
      setConfirm(null);
      await afterChange();
    } catch (e) {
      setConfirm(null);
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-3 p-4">
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          void create();
        }}
        aria-label="Create checkpoint"
      >
        <label className="flex min-w-48 flex-1 flex-col gap-1">
          <span className="label">Label (optional)</span>
          <input className="field" value={label} onChange={(e) => setLabel(e.target.value)} maxLength={200} placeholder="before refactor" />
        </label>
        <button type="submit" className="btn btn-primary" disabled={busy || active} title={active ? 'Pause the task before taking a manual checkpoint' : undefined}>
          <Save className="size-3.5" aria-hidden /> Create checkpoint
        </button>
      </form>
      {active && <p className="text-xs text-faint">The task is running: pause it before creating or restoring a checkpoint.</p>}
      {error ? <ErrorBox error={error} /> : null}
      <Async res={res} what="Checkpoints">
        {(cps) =>
          cps.length === 0 ? (
            <Empty title="No checkpoints yet" />
          ) : (
            <ul className="flex flex-col gap-3" aria-label="Checkpoints">
              {[...cps].reverse().map((c) => (
                <li key={c.id} className="pixel-card p-3" data-testid="checkpoint">
                  <div className="flex items-center justify-between gap-2 text-xs">
                    <span className="chip text-signal">{c.kind}</span>
                    {typeof c.summary.label === 'string' && <span className="truncate text-muted">{c.summary.label}</span>}
                    <span className="ml-auto text-faint">{formatDateTime(c.created_at)}</span>
                    <button
                      className="btn btn-sm"
                      disabled={busy || active || !c.git_ref}
                      title={!c.git_ref ? 'This checkpoint has no git ref to restore' : active ? 'Pause the task first' : 'Reset the task worktree to this checkpoint'}
                      onClick={() => setConfirm(c.id)}
                      aria-label={`Restore checkpoint ${c.id}`}
                    >
                      <History className="size-3.5" aria-hidden /> Restore
                    </button>
                  </div>
                  {c.git_ref && <div className="mt-1 font-mono text-[11px] text-muted">git {c.git_ref}</div>}
                  {typeof c.summary.git_error === 'string' && <div className="mt-1 text-xs text-danger">git: {c.summary.git_error}</div>}
                  <details className="mt-2">
                    <summary className="cursor-pointer text-xs text-muted">Summary</summary>
                    <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap font-mono text-[11px]">{JSON.stringify(c.summary, null, 2)}</pre>
                  </details>
                </li>
              ))}
            </ul>
          )
        }
      </Async>
      {confirm && (
        <Modal open onOpenChange={(o) => !o && setConfirm(null)} title="Restore checkpoint?" description="Resets the task worktree to the checkpoint's git ref.">
          <div className="flex flex-col gap-4 p-4">
            <p className="text-sm">
              Later and uncommitted changes in this task&apos;s worktree are discarded (<span className="font-mono">git reset --hard</span> in the task&apos;s own worktree; the project directory is not
              touched).
            </p>
            <div className="flex justify-end gap-2">
              <button className="btn" onClick={() => setConfirm(null)}>
                Cancel
              </button>
              <button className="btn btn-danger" onClick={() => void restore(confirm)} disabled={busy}>
                Restore worktree
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}

export function DiffTab({ taskId }: { taskId: string }) {
  const res = useTaskResource(taskId, () => api.taskDiff(taskId));
  if (noWorkspace(res.error)) return <Empty title="No workspace yet" hint="The task has not started, so there is no worktree to diff." />;
  return (
    <Async res={{ ...res, unavailable: false }} what="Diff">
      {(d) => {
        if (!d.available) return <Empty title="Diff not available" hint={d.reason ?? 'The workspace is not a git repository.'} />;
        return (
          <div>
            <div className="border-b border-line px-4 py-2 text-xs text-muted">
              <span className="font-mono">{d.branch ?? 'no branch'}</span>
              {d.base_commit && (
                <span className="ml-2 font-mono text-faint">
                  {d.base_commit.slice(0, 8)}..{(d.head_commit ?? '').slice(0, 8)}
                </span>
              )}
              <span className="ml-2">
                {d.changed_files.length} changed file{d.changed_files.length === 1 ? '' : 's'}
              </span>
              {d.changed_files.length > 0 && (
                <div className="mt-1 font-mono text-[11px] text-ink" data-testid="changed-files">
                  {d.changed_files.join(', ')}
                </div>
              )}
              {d.stat && <pre className="mt-1 whitespace-pre-wrap font-mono text-[11px] text-faint">{d.stat}</pre>}
            </div>
            {d.diff ? <DiffView diff={d.diff} /> : <Empty title="No changes recorded" hint="The task has no diff against its base commit." />}
          </div>
        );
      }}
    </Async>
  );
}

export function ArtifactsTab({ taskId }: { taskId: string }) {
  const res = useTaskResource(taskId, () => api.taskArtifacts(taskId));
  const [open, setOpen] = useState<string | null>(null);
  const file = useResource(() => (open ? api.taskArtifact(taskId, open) : Promise.resolve(null)), [taskId, open]);
  if (noWorkspace(res.error)) return <Empty title="No workspace yet" hint="The task has not started, so there are no files to show." />;
  return (
    <Async res={{ ...res, unavailable: false }} what="Artifacts">
      {(list) =>
        list.files.length === 0 ? (
          <Empty title="No changed files" hint="Files the task changed against its base commit appear here." />
        ) : (
          <div className="grid gap-3 p-4 md:grid-cols-[16rem_1fr]">
            <ul className="text-[13px]" aria-label="Changed files">
              {list.files.map((f) => (
                <li key={f.path} className="border-b border-line/60">
                  <button
                    className={`flex w-full items-center gap-2 py-1.5 text-left hover:text-phosphor ${open === f.path ? 'text-phosphor' : ''}`}
                    onClick={() => setOpen(f.path)}
                    disabled={!f.exists}
                    aria-pressed={open === f.path}
                    title={f.exists ? 'View file' : 'Deleted in the workspace'}
                  >
                    <FileText className="size-3.5 shrink-0" aria-hidden />
                    <span className="truncate font-mono">{f.path}</span>
                    <span className="tick-num ml-auto text-xs text-faint">{f.size === null ? 'deleted' : `${f.size} B`}</span>
                  </button>
                </li>
              ))}
            </ul>
            <div className="min-w-0">
              {!open ? (
                <div className="text-xs text-faint">Select a file to view it.</div>
              ) : file.error ? (
                <ErrorBox error={file.error} />
              ) : file.loading || !file.data ? (
                <div className="text-xs text-muted">Loading...</div>
              ) : (
                <div>
                  <div className="mb-1 font-mono text-xs text-muted">
                    {file.data.path} ({file.data.size} B)
                  </div>
                  <pre className="max-h-[28rem] overflow-auto whitespace-pre bg-void p-3 font-mono text-xs" aria-label={`Contents of ${file.data.path}`} data-testid="artifact-content">
                    {file.data.content}
                  </pre>
                </div>
              )}
            </div>
          </div>
        )
      }
    </Async>
  );
}
