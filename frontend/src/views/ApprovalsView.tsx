import { Check, X } from 'lucide-react';
import { useState } from 'react';
import { api } from '../api/endpoints';
import { Empty, ErrorBox, Unavailable, ViewHeader } from '../components/ui';
import type { Approval, VerifyCommand } from '../api/types';
import { VerifyChecks } from '../components/VerifyChecks';
import { formatDateTime } from '../lib/time';
import { pendingApprovals } from '../store/reducer';
import { useStore } from '../store/store';

const str = (v: unknown): string | null => (typeof v === 'string' && v ? v : null);
const strs = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []);

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt className="text-muted">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </>
  );
}

/** Exact artefact a skill/MCP activation writes (details.activation) plus the hashes the approval is bound to. */
function ActivationFacts({ details: d }: { details: Record<string, unknown> }) {
  const a = d.activation && typeof d.activation === 'object' ? (d.activation as Record<string, unknown>) : null;
  const short = (h: unknown) => (typeof h === 'string' && h ? h.slice(0, 16) : null);
  const content = short(d.content_sha256);
  const entry = short(d.mcp_entry_sha256);
  if (!a && !content && !entry) return null;
  const args = Array.isArray(a?.args) ? (a!.args as unknown[]).map(String) : [];
  const envKeys = strs(a?.env_keys);
  return (
    <>
      {a && (
        <Fact label="Writes">
          <span className="font-mono" data-testid="activation-target">{str(a.file) ?? str(a.folder) ?? 'unknown'}</span>
          {str(a.server) && <span> (server <span className="font-mono">{str(a.server)}</span>)</span>}
        </Fact>
      )}
      {a && str(a.command) && (
        <Fact label="Will run">
          <code className="break-all font-mono text-amber" data-testid="activation-command">{[str(a.command), ...args].join(' ')}</code>
          {str(a.transport) && <span className="ml-2 text-muted">transport {str(a.transport)}</span>}
        </Fact>
      )}
      {a && str(a.url) && (
        <Fact label="Server URL">
          <code className="break-all font-mono text-amber" data-testid="activation-url">{str(a.url)}</code>
          {str(a.transport) && <span className="ml-2 text-muted">transport {str(a.transport)}</span>}
        </Fact>
      )}
      {a && (str(a.command) || str(a.url)) && (
        <Fact label="Env keys"><span className="font-mono" data-testid="activation-env">{envKeys.length ? envKeys.join(', ') : 'none'}</span> <span className="text-muted">(names only)</span></Fact>
      )}
      {content && <Fact label="Content hash"><span className="font-mono" title={String(d.content_sha256)} data-testid="activation-content-hash">{content}</span></Fact>}
      {entry && <Fact label="MCP entry hash"><span className="font-mono" title={String(d.mcp_entry_sha256)} data-testid="activation-entry-hash">{entry}</span></Fact>}
    </>
  );
}

/** Readable facts for the approval kinds that carry structured details. The backend's own summary says what approving grants. */
export function ApprovalFacts({ approval }: { approval: Approval }) {
  const projects = useStore((s) => s.data.projects);
  const d = approval.details ?? {};
  const projectId = str(d.project_id);
  const projectName = projectId ? (projects[projectId]?.name ?? projectId) : null;
  if (approval.kind === 'untrusted_tool_install') {
    const findings = strs(d.findings);
    const reasons = strs(d.review_reasons);
    return (
      <dl className="grid grid-cols-[8rem_1fr] gap-x-3 gap-y-1 text-xs" aria-label="Skill install details" data-testid="approval-facts">
        <Fact label="Skill">{str(d.skill_name) ?? str(d.skill_id) ?? 'unknown'}</Fact>
        <Fact label="Ref"><span className="font-mono">{str(d.ref) ?? 'unknown'}</span></Fact>
        <Fact label="Project">{projectName ?? 'unknown'}</Fact>
        <Fact label="Kind of change">{str(d.mode) === 'mcp_server' ? 'Adds an MCP server entry to the project .mcp.json' : 'Copies the skill files into the project .claude/skills'}</Fact>
        <Fact label="Source"><span className="break-all font-mono">{str(d.source_url) ?? 'none'}</span></Fact>
        <Fact label="Risk">{str(d.risk) ?? 'unknown'}</Fact>
        <ActivationFacts details={d} />
        {reasons.length > 0 && (
          <Fact label="Review reasons">
            <ul className="list-inside list-disc">{reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
          </Fact>
        )}
        {findings.length > 0 && (
          <Fact label="Top findings">
            <ul className="list-inside list-disc">{findings.map((r, i) => <li key={i}>{r}</li>)}</ul>
          </Fact>
        )}
      </dl>
    );
  }
  if (approval.kind === 'leader_verify_commands') {
    const proposed = Array.isArray(d.verify_commands) ? (d.verify_commands as VerifyCommand[]) : [];
    const unsafe = Array.isArray(d.unsafe) ? (d.unsafe as { command?: unknown; reason?: unknown }[]) : [];
    return (
      <dl className="grid grid-cols-[8rem_1fr] gap-x-3 gap-y-1 text-xs" aria-label="Verify checks details" data-testid="approval-facts">
        <Fact label="Source">{str(d.source) === 'objective' ? 'Objective plan' : 'Leader task plan'}</Fact>
        <Fact label="Checks to adopt">{proposed.length ? <VerifyChecks checks={proposed} testId="approval-verify-checks" /> : <span className="text-faint">none listed</span>}</Fact>
        {unsafe.length > 0 && (
          <Fact label="Refused by the safety filter">
            <ul className="list-inside list-disc">
              {unsafe.map((u, i) => (
                <li key={i}>
                  <span className="font-mono">{typeof u.command === 'string' ? u.command : JSON.stringify(u.command)}</span>{str(u.reason) ? ` (${str(u.reason)})` : ''}
                </li>
              ))}
            </ul>
          </Fact>
        )}
      </dl>
    );
  }
  if (approval.kind === 'permission_escalation') {
    const grants = str(d.grants);
    return (
      <dl className="grid grid-cols-[8rem_1fr] gap-x-3 gap-y-1 text-xs" aria-label="Permission details" data-testid="approval-facts">
        <Fact label="Phase">{str(d.phase) ?? 'unknown'}</Fact>
        <Fact label="Provider">{str(d.provider) ?? 'unknown'}</Fact>
        <Fact label="Ran as">{str(d.permission) ?? 'unknown'}</Fact>
        <Fact label="Approving grants">{grants ? <span className="font-mono">{grants}</span> : <span className="text-amber">no additional permission</span>}</Fact>
      </dl>
    );
  }
  if (approval.kind === 'high_risk_task') {
    const dec = d.decision && typeof d.decision === 'object' ? (d.decision as Record<string, unknown>) : {};
    const reasons = strs(dec.reasons);
    return (
      <dl className="grid grid-cols-[8rem_1fr] gap-x-3 gap-y-1 text-xs" aria-label="High-risk task details" data-testid="approval-facts">
        <Fact label="Task type">{str(dec.task_type) ?? 'unknown'}</Fact>
        <Fact label="Would run on"><span className="font-mono">{str(dec.would_route_to) ?? (str(dec.provider) ? `${dec.provider}/${str(dec.model) ?? 'default'}` : 'not decided')}</span></Fact>
        {reasons.length > 0 && (
          <Fact label="Router reasons">
            <ul className="list-inside list-disc">{reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
          </Fact>
        )}
      </dl>
    );
  }
  return null;
}

export function ApprovalCard({ approval }: { approval: Approval }) {
  const putApproval = useStore((s) => s.putApproval);
  const task = useStore((s) => (approval.task_id ? s.data.tasks[approval.task_id] : undefined));
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const resolve = async (approve: boolean) => {
    setBusy(true);
    setError(null);
    try {
      const res = await api.resolveApproval(approval.id, approve, note);
      putApproval(res && res.id ? res : { ...approval, status: approve ? 'approved' : 'denied', note: note || null });
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <li className="pixel-card flex flex-col gap-3 p-4" aria-label={`Approval ${approval.kind}`}>
      <div className="flex items-start justify-between gap-3">
        <div>
          <span className="chip text-amber">{approval.kind}</span>
          <p className="mt-2 text-[13px]">{approval.summary}</p>
          {task && <p className="mt-1 text-xs text-muted">Task: {task.title}</p>}
        </div>
        <span className="text-xs text-faint">{formatDateTime(approval.requested_at)}</span>
      </div>
      <ApprovalFacts approval={approval} />
      {Object.keys(approval.details ?? {}).length > 0 && (
        <details>
          <summary className="cursor-pointer text-xs text-muted">Raw details</summary>
          <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap bg-void p-2 font-mono text-[11px]">{JSON.stringify(approval.details, null, 2)}</pre>
        </details>
      )}
      <div className="flex flex-wrap items-end gap-2">
        <label className="flex min-w-60 flex-1 flex-col gap-1">
          <span className="label">Note (optional)</span>
          <input className="field" value={note} onChange={(e) => setNote(e.target.value)} aria-label={`Note for approval ${approval.id}`} />
        </label>
        <button className="btn btn-primary" onClick={() => resolve(true)} disabled={busy}>
          <Check className="size-3.5" aria-hidden /> Approve
        </button>
        <button className="btn btn-danger" onClick={() => resolve(false)} disabled={busy}>
          <X className="size-3.5" aria-hidden /> Deny
        </button>
      </div>
      {error ? <ErrorBox error={error} /> : null}
    </li>
  );
}

export function ApprovalsView() {
  const data = useStore((s) => s.data);
  const unavailable = useStore((s) => s.unavailable.approvals);
  const pending = pendingApprovals(data);
  const resolved = Object.values(data.approvals)
    .filter((a) => a.status !== 'pending')
    .sort((a, b) => (b.resolved_at ?? '').localeCompare(a.resolved_at ?? ''));
  return (
    <div>
      <ViewHeader title="Approvals" subtitle="Human gates: destructive git ops, deploys, paid activation, untrusted installs" />
      {unavailable ? (
        <Unavailable what="Approvals" />
      ) : (
        <div className="flex flex-col gap-6 px-5 pb-8">
          {pending.length === 0 ? (
            <Empty title="Nothing waiting for approval" />
          ) : (
            <ul className="flex flex-col gap-3" aria-label="Pending approvals">
              {pending.map((a) => (
                <ApprovalCard key={a.id} approval={a} />
              ))}
            </ul>
          )}
          {resolved.length > 0 && (
            <section>
              <h2 className="label mb-2">Resolved this session</h2>
              <ul className="flex flex-col gap-1 text-[13px]">
                {resolved.map((a) => (
                  <li key={a.id} className="flex items-center gap-3 border-b border-line/60 py-1.5">
                    <span className="chip" style={{ color: a.status === 'approved' ? 'var(--color-ok)' : 'var(--color-danger)' }}>
                      {a.status}
                    </span>
                    <span className="truncate">{a.summary}</span>
                    {a.note && <span className="text-xs text-muted">&ldquo;{a.note}&rdquo;</span>}
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>
      )}
    </div>
  );
}
