import * as Tabs from '@radix-ui/react-tabs';
import { ArrowDownToLine, Pause, Play } from 'lucide-react';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { api } from '../../api/endpoints';
import type { Task } from '../../api/types';
import { Async, Duration, Empty, ExactnessChip, Modal, StateBadge } from '../../components/ui';
import { formatDateTime, formatDuration, formatTime, relativeAgo, useNow } from '../../lib/time';
import { VerifyChecks, VerifyOriginChip } from '../../components/VerifyChecks';
import { planVerifyInfo } from '../../lib/verify';
import { activeSeconds } from '../../store/reducer';
import { useStore } from '../../store/store';
import { TaskActions } from './TaskActions';
import { ArtifactsTab, CheckpointsTab, DiffTab, TimelineTab } from './TaskTabs';
import { useTaskResource } from './useTaskResource';

const TABS = ['overview', 'log', 'timeline', 'runs', 'checkpoints', 'diff', 'verification', 'artifacts'] as const;
const TAB_LABEL: Record<(typeof TABS)[number], string> = {
  overview: 'Overview',
  log: 'Live log',
  timeline: 'Timeline',
  runs: 'Runs',
  checkpoints: 'Checkpoints',
  diff: 'Diff',
  verification: 'Verification',
  artifacts: 'Artifacts',
};

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[9rem_1fr] gap-3 border-b border-line/60 py-1.5 text-[13px]">
      <dt className="label pt-0.5">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  );
}

function Unknown() {
  return <span className="text-faint">not recorded</span>;
}

function StepList({ steps }: { steps: unknown[] }) {
  if (!steps.length) return <Unknown />;
  return (
    <ul className="list-inside list-disc">
      {steps.map((s, i) => (
        <li key={i}>{typeof s === 'string' ? s : JSON.stringify(s)}</li>
      ))}
    </ul>
  );
}

function Overview({ task }: { task: Task }) {
  const project = useStore((s) => s.data.projects[task.project_id]);
  const clock = useStore((s) => s.data.clocks[task.id]);
  const runs = useTaskResource(task.id, () => api.taskRuns(task.id));
  const verifications = useTaskResource(task.id, () => api.taskVerification(task.id));
  const lastRun = runs.data && runs.data.length ? runs.data[runs.data.length - 1] : null;
  const lastVerification = verifications.data && verifications.data.length ? verifications.data[verifications.data.length - 1].status : null;
  const verification = useStore((s) => s.data.verification[task.id]) ?? lastVerification ?? task.verification_status ?? null;
  const now = useNow();
  const planInfo = planVerifyInfo(task.plan);
  const usage = task.usage && Object.keys(task.usage).length > 0 ? task.usage : null;
  return (
    <dl className="px-4 py-2">
      <Row label="Title">{task.title}</Row>
      <Row label="Description">{task.description ? <span className="whitespace-pre-wrap">{task.description}</span> : <Unknown />}</Row>
      <Row label="Project">{project?.name ?? task.project_id}</Row>
      <Row label="State">
        <span className="inline-flex flex-wrap items-center gap-2">
          <StateBadge state={task.state} />
          {task.state_reason && <span className="text-muted">{task.state_reason}</span>}
        </span>
      </Row>
      <Row label="Priority">{task.priority}</Row>
      <Row label="Type / role">
        <span className="font-mono">{task.task_type}</span> / <span className="font-mono">{task.role ?? 'auto'}</span>
      </Row>
      <Row label="Worker">{task.agent_id ?? <Unknown />}</Row>
      <Row label="Provider / model">
        {task.provider || task.model || lastRun ? (
          <span className="font-mono" data-testid="provider-model">
            {task.provider ?? lastRun?.provider ?? '?'} / {task.model ?? lastRun?.model ?? '?'}
          </span>
        ) : task.preferred_provider || task.preferred_model ? (
          <span>
            <span className="font-mono">
              {task.preferred_provider ?? '?'} / {task.preferred_model ?? '?'}
            </span>{' '}
            <span className="chip text-faint">preferred</span>
          </span>
        ) : (
          <Unknown />
        )}
      </Row>
      <Row label="Routing">
        <a href={`#/routing?task=${encodeURIComponent(task.id)}`} className="text-signal underline" data-testid="routing-link">
          Why this model? See this task&apos;s routing decisions
        </a>
      </Row>
      {task.objective_id && (
        <Row label="Objective">
          <a href={`#/mission?objective=${encodeURIComponent(task.objective_id)}`} className="text-signal underline" data-testid="objective-link">
            Part of objective {task.objective_id}
          </a>
        </Row>
      )}
      <Row label="Started">{formatDateTime(task.started_at)}</Row>
      <Row label="Active time">
        <Duration seconds={activeSeconds(clock, task, now)} />
        <span className="ml-2 text-[11px] text-faint">{clock?.exact ? 'ticks locally from the last recorded transition' : 'resolving from transitions...'}</span>
      </Row>
      <Row label="Last activity">
        {task.last_activity_at ? `${formatDateTime(task.last_activity_at)} (${relativeAgo(task.last_activity_at, now)})` : <Unknown />}
      </Row>
      <Row label="Current operation">{task.current_operation ? <span className="font-mono text-phosphor">{task.current_operation}</span> : <Unknown />}</Row>
      <Row label="Verification">{verification ? <span className="chip" data-testid="overview-verification" style={{ color: verification === 'passed' ? 'var(--color-ok)' : 'var(--color-amber)' }}>{verification}</span> : <Unknown />}</Row>
      <Row label="Next action">{task.next_action ?? <Unknown />}</Row>
      <Row label="Remaining work">
        <StepList steps={task.pending_steps} />
      </Row>
      <Row label="Completed steps">
        <StepList steps={task.completed_steps} />
      </Row>
      <Row label="Acceptance">{task.acceptance_criteria.length ? <StepList steps={task.acceptance_criteria} /> : <Unknown />}</Row>
      <Row label="Verify commands">
        {task.verify_commands.length ? (
          <VerifyChecks checks={task.verify_commands} testId="task-verify-commands" />
        ) : (
          <Unknown />
        )}
      </Row>
      {(planInfo.origin || planInfo.notAdopted) && (
        <Row label="Verify origin">
          <span className="flex flex-col gap-1">
            {planInfo.origin && <VerifyOriginChip origin={planInfo.origin} />}
            {planInfo.notAdopted && (
              <span className="text-xs text-amber" data-testid="verify-not-adopted">
                Leader checks not adopted: {planInfo.notAdopted}
              </span>
            )}
          </span>
        </Row>
      )}
      <Row label="Dependencies">{task.depends_on.length ? <span className="font-mono text-xs">{task.depends_on.join(', ')}</span> : <Unknown />}</Row>
      <Row label="Failures">
        <span className="tick-num">
          attempts {task.attempt_count}, consecutive failures {task.consecutive_failures}, repair cycles {task.repair_cycles}
        </span>
      </Row>
      <Row label="Branch">{task.branch ? <span className="font-mono text-xs">{task.branch}</span> : <Unknown />}</Row>
      <Row label="Usage">
        {usage ? (
          <span className="inline-flex items-center gap-2">
            <code className="text-xs">{JSON.stringify(usage)}</code>
            <ExactnessChip exactness="unknown" />
          </span>
        ) : (
          <span className="text-faint">unknown (no measured usage recorded)</span>
        )}
      </Row>
    </dl>
  );
}

function LiveLog({ taskId }: { taskId: string }) {
  const lines = useStore((s) => s.data.logs[taskId]);
  const loadTaskLog = useStore((s) => s.loadTaskLog);
  const [paused, setPaused] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    void loadTaskLog(taskId);
  }, [taskId, loadTaskLog]);
  const count = lines?.length ?? 0;
  useEffect(() => {
    if (!paused && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [count, paused]);
  return (
    <div className="flex h-full min-h-72 flex-col">
      <div className="flex items-center justify-between border-b border-line px-4 py-2">
        <span className="text-xs text-muted tick-num">{count} lines</span>
        <button className="btn btn-sm" onClick={() => setPaused((p) => !p)} aria-pressed={paused}>
          {paused ? <Play className="size-3.5" aria-hidden /> : <Pause className="size-3.5" aria-hidden />}
          {paused ? 'Resume autoscroll' : 'Pause autoscroll'}
        </button>
        {paused && (
          <button className="btn btn-sm" onClick={() => { setPaused(false); }}>
            <ArrowDownToLine className="size-3.5" aria-hidden /> Jump to end
          </button>
        )}
      </div>
      <div
        ref={box}
        role="log"
        aria-live="off"
        aria-label="Live task output"
        className="min-h-0 flex-1 overflow-auto bg-void p-3 font-mono text-xs leading-relaxed"
        onWheel={(e) => {
          if (e.deltaY < 0) setPaused(true);
        }}
        tabIndex={0}
      >
        {count === 0 ? (
          <span className="text-faint">No output recorded for this task yet.</span>
        ) : (
          lines!.map((l) =>
            l.kind === 'tool' ? (
              <div key={l.seq} className="text-amber">
                <span className="text-faint">{formatTime(l.ts)} </span>
                {'> '}
                {l.tool}
                {l.text ? `: ${l.text}` : ''}
              </div>
            ) : (
              <div key={l.seq} className="whitespace-pre-wrap break-words">
                <span className="text-faint">{formatTime(l.ts)} </span>
                {l.text}
              </div>
            ),
          )
        )}
      </div>
    </div>
  );
}

function RunsTab({ taskId }: { taskId: string }) {
  const res = useTaskResource(taskId, () => api.taskRuns(taskId));
  return (
    <Async res={res} what="Runs">
      {(runs) =>
        runs.length === 0 ? (
          <Empty title="No runs yet" />
        ) : (
          <div className="overflow-x-auto p-3">
            <table className="w-full text-left text-xs">
              <thead className="label">
                <tr>
                  {['Phase', 'Status', 'Provider / model', 'Started', 'Duration', 'Exit', 'Error'].map((h) => (
                    <th key={h} className="px-2 py-1 font-normal">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => {
                  const dur = r.finished_at ? (Date.parse(r.finished_at) - Date.parse(r.started_at)) / 1000 : null;
                  return (
                    <tr key={r.id} className="border-t border-line/60">
                      <td className="px-2 py-1.5 font-mono">{r.phase}</td>
                      <td className="px-2 py-1.5">{r.status}</td>
                      <td className="px-2 py-1.5 font-mono">
                        {r.provider} / {r.model ?? 'default'}
                      </td>
                      <td className="px-2 py-1.5">{formatDateTime(r.started_at)}</td>
                      <td className="tick-num px-2 py-1.5">{dur === null ? 'running / unknown' : formatDuration(dur)}</td>
                      <td className="tick-num px-2 py-1.5">{r.exit_code ?? '--'}</td>
                      <td className="px-2 py-1.5 text-danger">{r.error_category ? `${r.error_category}: ${r.error_message ?? ''}` : ''}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )
      }
    </Async>
  );
}

function VerificationTab({ taskId }: { taskId: string }) {
  const res = useTaskResource(taskId, () => api.taskVerification(taskId));
  return (
    <Async res={res} what="Verification">
      {(v) => {
        const list = [...v].reverse();
        if (!list.length) return <Empty title="No verification recorded" hint="Verification runs in the TESTING phase." />;
        return (
          <div className="flex flex-col gap-4 p-4">
            {list.map((ver) => (
              <section key={ver.id} className="pixel-card p-3" aria-label={`Verification ${ver.status}`}>
                <div className="mb-2 flex items-center justify-between">
                  <span className="chip" style={{ color: ver.status === 'passed' ? 'var(--color-ok)' : 'var(--color-amber)' }}>
                    {ver.status}
                  </span>
                  <span className="flex items-center gap-2 text-xs text-faint">
                    <span className="label">checks by</span>
                    <VerifyOriginChip origin={ver.origin} />
                    {formatDateTime(ver.started_at)}
                  </span>
                </div>
                {ver.checks.length === 0 && <div className="text-xs text-faint">No checks were configured.</div>}
                {ver.checks.map((c, i) => (
                  <div key={i} className="mt-2 border-t border-line/60 pt-2">
                    <div className="flex flex-wrap items-center gap-2 text-[13px]">
                      <span className={c.passed ? 'text-ok' : 'text-danger'}>{c.passed ? 'PASS' : 'FAIL'}</span>
                      <span className="font-mono">{c.name}</span>
                      <span className="tick-num text-xs text-muted">exit {c.exit_code ?? '--'}</span>
                      {c.timed_out && <span className="chip text-amber">timed out</span>}
                      <span className="tick-num text-xs text-faint">{formatDuration(c.duration_seconds)}</span>
                    </div>
                    {c.argv && <div className="font-mono text-[11px] text-muted">$ {c.argv.join(' ')}</div>}
                    {c.error && <div className="text-xs text-danger">{c.error}</div>}
                    {c.output && <pre className="mt-1 max-h-48 overflow-auto bg-void p-2 font-mono text-[11px]">{c.output}</pre>}
                  </div>
                ))}
              </section>
            ))}
          </div>
        );
      }}
    </Async>
  );
}

export function TaskDetail({ taskId, onClose }: { taskId: string; onClose: () => void }) {
  const task = useStore((s) => s.data.tasks[taskId]);
  const putTask = useStore((s) => s.putTask);
  const [tab, setTab] = useState<(typeof TABS)[number]>('overview');

  // Refresh the full record once when the drawer opens (list rows may be partial).
  useEffect(() => {
    let off = false;
    api
      .task(taskId)
      .then((t) => !off && putTask(t))
      .catch(() => undefined);
    return () => {
      off = true;
    };
  }, [taskId, putTask]);

  return (
    <Modal open onOpenChange={(o) => !o && onClose()} title={task?.title ?? taskId} description={task ? `${task.id}` : undefined} side="right">
      {!task ? (
        <div className="p-4">
          <Empty title="Task not found" hint="It may have been deleted." />
        </div>
      ) : (
        <div className="flex h-full flex-col">
          <div className="border-b border-line px-4 py-2">
            <TaskActions task={task} />
          </div>
          <Tabs.Root value={tab} onValueChange={(v) => setTab(v as (typeof TABS)[number])} className="flex min-h-0 flex-1 flex-col">
            <Tabs.List aria-label="Task sections" className="flex flex-wrap gap-0.5 border-b border-line px-3">
              {TABS.map((t) => (
                <Tabs.Trigger
                  key={t}
                  value={t}
                  className="display border-b-2 border-transparent px-2.5 py-2 text-[11px] uppercase tracking-wider text-muted hover:text-ink data-[state=active]:border-phosphor data-[state=active]:text-phosphor"
                >
                  {TAB_LABEL[t]}
                </Tabs.Trigger>
              ))}
            </Tabs.List>
            <div className="min-h-0 flex-1 overflow-auto">
              <Tabs.Content value="overview">
                <Overview task={task} />
              </Tabs.Content>
              <Tabs.Content value="log" className="h-full">
                <LiveLog taskId={taskId} />
              </Tabs.Content>
              <Tabs.Content value="timeline">
                <TimelineTab taskId={taskId} />
              </Tabs.Content>
              <Tabs.Content value="runs">
                <RunsTab taskId={taskId} />
              </Tabs.Content>
              <Tabs.Content value="checkpoints">
                <CheckpointsTab task={task} />
              </Tabs.Content>
              <Tabs.Content value="diff">
                <DiffTab taskId={taskId} />
              </Tabs.Content>
              <Tabs.Content value="verification">
                <VerificationTab taskId={taskId} />
              </Tabs.Content>
              <Tabs.Content value="artifacts">
                <ArtifactsTab taskId={taskId} />
              </Tabs.Content>
            </div>
          </Tabs.Root>
        </div>
      )}
    </Modal>
  );
}
