import { Link } from 'react-router-dom';
import { Download, FileText, Power, PowerOff, RefreshCw, Undo2 } from 'lucide-react';
import { useEffect, useState, type ReactNode } from 'react';
import { api } from '../../api/endpoints';
import type { Project, SkillActionName, SkillActionResult, SkillContent, SkillDetail, SkillFinding, SkillOutcome } from '../../api/types';
import { ErrorBox, Modal, Spinner } from '../../components/ui';
import { formatDateTime } from '../../lib/time';
import { useResource } from '../../lib/useResource';
import { useStore } from '../../store/store';
import {
  CATALOGUE_ONLY_KINDS,
  CompatChips,
  CopyButton,
  DOWNLOADED_STATES,
  INSTALLABLE_FROM,
  KIND_LABEL,
  PriorityMark,
  RiskChip,
  SkillStateBadge,
  SourceLink,
  SourceMark,
} from './parts';

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-1.5 border-b border-line/60 px-4 py-3" aria-label={title}>
      <h3 className="label">{title}</h3>
      {children}
    </section>
  );
}

const asText = (v: unknown): string => (typeof v === 'string' ? v : JSON.stringify(v));
const short = (r: string | null | undefined) => (r ? r.slice(0, 12) : '--');

function Plain({ items, empty = 'none' }: { items: unknown[]; empty?: string }) {
  if (!items.length) return <span className="text-xs text-faint">{empty}</span>;
  return (
    <ul className="list-inside list-disc text-xs">
      {items.map((x, i) => (
        <li key={i}>{asText(x)}</li>
      ))}
    </ul>
  );
}

const SEV_ORDER = ['high', 'medium', 'low'];
function Findings({ findings }: { findings: SkillFinding[] }) {
  if (!findings.length) return <p className="text-xs text-faint">No findings.</p>;
  const sorted = [...findings].sort((a, b) => SEV_ORDER.indexOf(a.severity) - SEV_ORDER.indexOf(b.severity));
  return (
    <ul className="flex flex-col gap-1 text-xs" aria-label="Findings">
      {sorted.map((f, i) => (
        <li key={i} className="flex gap-2" data-severity={f.severity}>
          <span className={`chip shrink-0 ${f.severity === 'high' ? 'text-danger' : f.severity === 'medium' ? 'text-amber' : 'text-faint'}`}>{f.severity}</span>
          <span>
            {f.message} <span className="font-mono text-faint">{f.path}</span>
          </span>
        </li>
      ))}
    </ul>
  );
}

function SecuritySection({ s }: { s: SkillDetail }) {
  const sec = s.security;
  return (
    <Section title="Security report">
      <div className="flex flex-wrap items-center gap-2">
        <RiskChip risk={sec.risk ?? s.risk} />
        {sec.requires_credentials && <span className="chip text-amber">requires credentials</span>}
        {s.paid_service && s.paid_service !== 'no' && <span className="chip text-amber">paid service: {s.paid_service}</span>}
      </div>
      {!sec.inspected ? (
        <p className="text-xs text-muted">
          Not inspected yet. The files are inspected statically after download; until then only the catalogue risk is known.
          {sec.catalog_notes ? ` Catalogue notes: ${sec.catalog_notes}` : ''}
        </p>
      ) : (
        <>
          {(sec.review_reasons ?? []).length > 0 && (
            <div className="text-xs">
              <span className="text-amber">Review reasons:</span> <Plain items={sec.review_reasons ?? []} />
            </div>
          )}
          <Findings findings={sec.findings ?? []} />
          <dl className="grid grid-cols-[9rem_1fr] gap-x-3 gap-y-1 text-xs">
            <dt className="text-muted">Scripts</dt>
            <dd><Plain items={sec.scripts ?? []} /></dd>
            <dt className="text-muted">Native binaries</dt>
            <dd><Plain items={sec.native_binaries ?? []} /></dd>
            <dt className="text-muted">Hooks</dt>
            <dd><Plain items={sec.hooks ?? []} /></dd>
            <dt className="text-muted">Package scripts</dt>
            <dd><Plain items={(sec.package_scripts ?? []).map((p) => `${p.name}: ${p.command} (${p.path})`)} /></dd>
            <dt className="text-muted">MCP servers</dt>
            <dd><Plain items={(sec.mcp_servers ?? []).map((m) => `${m.name}: ${m.command} ${m.args.join(' ')} (${m.path})`)} /></dd>
          </dl>
        </>
      )}
    </Section>
  );
}

type Confirm = { action: SkillActionName | 'source'; title: string; body: ReactNode; label: string } | null;

export function SkillDrawer({ skillId, defaultProjectId, onClose, onChanged }: { skillId: string; defaultProjectId: string; onClose: () => void; onChanged: () => void }) {
  const rev = useStore((s) => s.data.rev.skills);
  const projects = useStore((s) => s.data.projects);
  const projectList = Object.values(projects);
  const [projectId, setProjectId] = useState(defaultProjectId || projectList[0]?.id || '');
  useEffect(() => {
    if (!projectId && projectList[0]) setProjectId(projectList[0].id);
  }, [projectId, projectList]);
  const res = useResource(() => api.skill(skillId), [skillId, rev]);
  const [override, setOverride] = useState<SkillDetail | null>(null);
  const skill = override && override.id === skillId ? override : res.data;
  useEffect(() => setOverride(null), [rev, skillId]);
  const [confirm, setConfirm] = useState<Confirm>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<{ outcome: SkillOutcome; action: string } | null>(null);
  const [content, setContent] = useState<SkillContent | null>(null);
  const [contentError, setContentError] = useState<unknown>(null);
  const [sourceDraft, setSourceDraft] = useState('');

  const project: Project | undefined = projects[projectId];
  const activeHere = skill?.activations.find((a) => a.project_id === projectId);

  const run = async (action: SkillActionName | 'source') => {
    if (!skill) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      let result: SkillActionResult;
      if (action === 'source') result = await api.setSkillSource(skill.id, sourceDraft.trim());
      else if (action === 'activate') result = await api.skillAction(skill.id, 'activate', { project_id: projectId, scope: 'project', targets: ['claude'] });
      else if (action === 'deactivate') result = await api.skillAction(skill.id, 'deactivate', { project_id: projectId });
      else result = await api.skillAction(skill.id, action);
      setOverride(result.skill);
      setNotice({ outcome: result.outcome, action });
      if (action === 'install' || action === 'update' || action === 'rollback') setContent(null);
      onChanged();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
      setConfirm(null);
    }
  };

  const loadContent = async () => {
    setContentError(null);
    try {
      setContent(await api.skillContent(skillId));
    } catch (e) {
      setContentError(e);
    }
  };

  const ask = (c: Confirm) => setConfirm(c);

  return (
    <Modal open onOpenChange={(o) => !o && onClose()} title={skill?.name ?? 'Skill'} description={skill ? `${KIND_LABEL[skill.kind] ?? skill.kind} · ${skill.category ?? 'uncategorised'}` : undefined} side="right">
      {!skill ? (
        <div className="p-4">{res.error ? <ErrorBox error={res.error} /> : <Spinner />}</div>
      ) : (
        <div data-testid="skill-detail" data-skill-id={skill.id}>
          <div className="flex flex-wrap items-center gap-2 border-b border-line/60 px-4 py-3">
            <SkillStateBadge state={skill.state} />
            <SourceMark skill={skill} />
            <RiskChip risk={skill.risk} />
            {skill.priority_candidate && <PriorityMark />}
            <CompatChips compat={skill.compat} />
          </div>

          <Section title="About">
            <p className="whitespace-pre-wrap text-[13px]">{skill.description || 'No description.'}</p>
            <dl className="grid grid-cols-[8rem_1fr] gap-x-3 gap-y-1 text-xs">
              <dt className="text-muted">License</dt>
              <dd>{skill.license ?? <span className="text-faint">unknown</span>}</dd>
              <dt className="text-muted">Maintenance</dt>
              <dd>
                {Object.keys(skill.maintenance).length ? (
                  Object.entries(skill.maintenance).map(([k, v]) => (
                    <span key={k} className="mr-3">
                      {k}: <span className="font-mono">{asText(v)}</span>
                    </span>
                  ))
                ) : (
                  <span className="text-faint">not recorded</span>
                )}
              </dd>
              <dt className="text-muted">Refs</dt>
              <dd className="font-mono">
                installed {short(skill.installed_ref)} · pinned {short(skill.pinned_ref)} · previous {short(skill.previous_ref)}
              </dd>
            </dl>
            {skill.warnings.length > 0 && (
              <ul className="text-xs text-amber" aria-label="Warnings">
                {skill.warnings.map((w, i) => (
                  <li key={i}>
                    {w.type === 'conflict' ? 'Conflict' : 'Duplicate'}: {w.reason}. Related: {w.with.join(', ')}
                  </li>
                ))}
              </ul>
            )}
            {skill.last_error && <p className="text-xs text-danger">Last error: {skill.last_error}</p>}
            {skill.incompatible_reasons.length > 0 && (
              <div className="text-xs text-danger">
                Incompatible: <Plain items={skill.incompatible_reasons} />
              </div>
            )}
          </Section>

          <Section title="Source">
            {skill.source_url ? <SourceLink url={skill.source_url} /> : <p className="text-xs text-amber">No source URL is known. It cannot be downloaded until one is set.</p>}
            {skill.uncertainty && (
              <p className="text-xs text-muted" data-testid="uncertainty">
                {skill.uncertainty}
              </p>
            )}
            {skill.candidates.length > 0 && (
              <div className="text-xs" data-testid="candidates">
                <div className="text-muted">Candidate sources:</div>
                <ul className="flex flex-col gap-1">
                  {skill.candidates.map((c) => (
                    <li key={c} className="flex flex-wrap items-center gap-2">
                      <span className="break-all font-mono">{c}</span>
                      <CopyButton text={c} label={`Copy candidate ${c}`} />
                      {INSTALLABLE_FROM.includes(skill.state) && (
                        <button className="btn btn-sm" onClick={() => setSourceDraft(c)}>
                          Use as source
                        </button>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {INSTALLABLE_FROM.includes(skill.state) && (!skill.source_url || skill.has_uncertainty) && (
              <form
                className="flex flex-wrap items-end gap-2"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (sourceDraft.trim().length >= 8)
                    ask({
                      action: 'source',
                      title: 'Set the source URL?',
                      label: 'Set source',
                      body: (
                        <p>
                          Stores <span className="break-all font-mono">{sourceDraft.trim()}</span> as the source of this entry (marked as set by you, not verified). Nothing is downloaded yet; the backend rejects anything that is not an allowed source.
                        </p>
                      ),
                    });
                }}
              >
                <label className="flex min-w-60 flex-1 flex-col gap-1">
                  <span className="label">Source URL</span>
                  <input className="field" value={sourceDraft} onChange={(e) => setSourceDraft(e.target.value)} placeholder="https://github.com/owner/repo" aria-label="Source URL" />
                </label>
                <button type="submit" className="btn" disabled={busy || sourceDraft.trim().length < 8}>
                  Set source
                </button>
              </form>
            )}
          </Section>

          <Section title="Install method">
            <dl className="grid grid-cols-[8rem_1fr] gap-x-3 gap-y-1 text-xs">
              <dt className="text-muted">Documented method</dt>
              <dd>{skill.install_method.type ?? <span className="text-faint">not documented</span>}</dd>
              <dt className="text-muted">Documented command</dt>
              <dd>
                {skill.install_method.command ? (
                  <span className="flex flex-wrap items-center gap-2">
                    <code className="break-all bg-void px-1.5 py-0.5 font-mono" data-testid="documented-command">
                      {skill.install_method.command}
                    </code>
                    <CopyButton text={skill.install_method.command} label="Copy documented command" />
                  </span>
                ) : (
                  <span className="text-faint">none</span>
                )}
              </dd>
              <dt className="text-muted">Notes</dt>
              <dd>{skill.install_method.notes ?? <span className="text-faint">none</span>}</dd>
            </dl>
            <p className="text-[11px] text-faint">{skill.instructions.note} The dashboard never runs this command.</p>
            <dl className="grid grid-cols-[8rem_1fr] gap-x-3 gap-y-1 text-xs">
              <dt className="text-muted">Dependencies</dt>
              <dd><Plain items={skill.dependencies} /></dd>
              <dt className="text-muted">Permissions</dt>
              <dd><Plain items={skill.permissions} /></dd>
            </dl>
          </Section>

          <SecuritySection s={skill} />

          <Section title="Activation">
            <div className="flex flex-wrap items-center gap-2">
              <label className="flex items-center gap-2 text-xs">
                <span className="label">Project</span>
                <select className="field w-auto" aria-label="Project for activation" value={projectId} onChange={(e) => setProjectId(e.target.value)}>
                  {projectList.length === 0 && <option value="">No projects</option>}
                  {projectList.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
              </label>
              <span className={`text-xs ${activeHere ? 'text-ok' : 'text-faint'}`} data-testid="active-here">
                {activeHere ? `Active in ${project?.name ?? projectId} (ref ${short(activeHere.ref)})` : `Not active in ${project?.name ?? 'this project'}`}
              </span>
            </div>
            {skill.activations.length > 0 && (
              <ul className="flex flex-col gap-1 text-xs" aria-label="Activations">
                {skill.activations.map((a) => (
                  <li key={a.project_id} className="border border-line bg-sunken px-2 py-1">
                    <span className="font-semibold">{projects[a.project_id]?.name ?? a.project_id}</span> · ref <span className="font-mono">{short(a.ref)}</span> · activated {formatDateTime(a.activated_at)}
                    <div className="font-mono text-[11px] text-muted">{a.artifacts.map((x) => x.path).join(', ')}</div>
                  </li>
                ))}
              </ul>
            )}
            <p className="text-xs" data-testid="codex-support">
              <span className="text-muted">Codex: </span>
              {skill.codex.supported ? <span className="text-ok">supported at {skill.codex.location}</span> : <span className="text-faint">unsupported — {skill.codex.reason}</span>}
            </p>
          </Section>

          <Section title="Actions">
            {skill.activation.mode === 'catalogue_only' || CATALOGUE_ONLY_KINDS.includes(skill.kind) ? (
              <div className="text-xs" data-testid="catalogue-only">
                <p className="text-amber">Catalogue only: this entry cannot be activated by the orchestrator.</p>
                {skill.activation.reason && <p className="text-muted">{skill.activation.reason}</p>}
                <p className="mt-1 text-muted">Follow the documented instructions yourself:</p>
                <p>{skill.install_method.notes ?? 'No notes recorded.'}</p>
              </div>
            ) : (
              <>
                <div className="flex flex-wrap gap-2">
                  {(INSTALLABLE_FROM.includes(skill.state) || skill.state === 'CATALOGUED_ON_DEMAND') && (
                    <button
                      className="btn btn-primary"
                      disabled={busy || !skill.installable}
                      title={skill.installable ? undefined : (skill.installable_reason ?? 'Not installable')}
                      onClick={() =>
                        ask({
                          action: 'install',
                          title: `Download ${skill.name}?`,
                          label: 'Download',
                          body: (
                            <p>
                              Fetches the repository at a pinned commit from <span className="break-all font-mono">{skill.source_url}</span> into the orchestrator&apos;s own skills folder. The files are listed and written by the orchestrator;
                              <b> nothing from the package is executed</b> and nothing is written into any project.
                            </p>
                          ),
                        })
                      }
                    >
                      <Download className="size-3.5" aria-hidden /> Download
                    </button>
                  )}
                  {DOWNLOADED_STATES.includes(skill.state) && skill.activation.possible && !activeHere && (
                    <button
                      className="btn btn-primary"
                      disabled={busy || !projectId}
                      onClick={() =>
                        ask({
                          action: 'activate',
                          title: `Activate ${skill.name} for ${project?.name ?? 'the project'}?`,
                          label: 'Activate',
                          body: (
                            <div className="flex flex-col gap-2">
                              <p>
                                Copies the downloaded files (ref <span className="font-mono">{short(skill.installed_ref)}</span>) into{' '}
                                <span className="font-mono">{project?.name}/{skill.activation.folder ?? skill.activation.file}</span>. Only that project is touched; the orchestrator never writes global configuration and never runs the package.
                              </p>
                              <p className="text-amber">
                                An <span className="font-mono">untrusted_tool_install</span> approval is required first. If none exists yet, one is requested and nothing is written; approve it in Approvals, then press Activate again.
                              </p>
                            </div>
                          ),
                        })
                      }
                    >
                      <Power className="size-3.5" aria-hidden /> Activate
                    </button>
                  )}
                  {activeHere && (
                    <button
                      className="btn btn-danger"
                      disabled={busy}
                      onClick={() =>
                        ask({
                          action: 'deactivate',
                          title: `Deactivate ${skill.name} in ${project?.name}?`,
                          label: 'Deactivate',
                          body: <p>Removes only the files this activation wrote into {project?.name}. The downloaded copy stays, so it can be activated again.</p>,
                        })
                      }
                    >
                      <PowerOff className="size-3.5" aria-hidden /> Deactivate
                    </button>
                  )}
                  {DOWNLOADED_STATES.includes(skill.state) && (
                    <button
                      className="btn"
                      disabled={busy || !skill.installable}
                      onClick={() =>
                        ask({
                          action: 'update',
                          title: `Update ${skill.name}?`,
                          label: 'Update',
                          body: (
                            <p>
                              Fetches the newest commit of the source. Copies active in projects are removed and re-activating the new ref needs a new approval. The current ref ({short(skill.installed_ref)}) is kept as the rollback target. Nothing is executed.
                            </p>
                          ),
                        })
                      }
                    >
                      <RefreshCw className="size-3.5" aria-hidden /> Update
                    </button>
                  )}
                  {skill.previous_ref && (
                    <button
                      className="btn"
                      disabled={busy}
                      onClick={() =>
                        ask({
                          action: 'rollback',
                          title: `Roll back ${skill.name}?`,
                          label: 'Roll back',
                          body: (
                            <p>
                              Goes back to the previous ref <span className="font-mono">{short(skill.previous_ref)}</span>. Projects that had an approved approval for that ref get it re-activated; the others are listed as skipped.
                            </p>
                          ),
                        })
                      }
                    >
                      <Undo2 className="size-3.5" aria-hidden /> Roll back
                    </button>
                  )}
                </div>
                {!skill.installable && INSTALLABLE_FROM.includes(skill.state) && (
                  <p className="text-xs text-amber" data-testid="not-installable">
                    Cannot be downloaded: {skill.installable_reason}
                  </p>
                )}
                {skill.activation.possible === false && DOWNLOADED_STATES.includes(skill.state) && (
                  <p className="text-xs text-amber">Cannot be activated: {skill.activation.reason}</p>
                )}
              </>
            )}
            {error ? <ErrorBox error={error} /> : null}
            {notice && <OutcomePanel n={notice} />}
          </Section>

          <Section title="SKILL.md">
            {DOWNLOADED_STATES.includes(skill.state) ? (
              <>
                <button className="btn btn-sm self-start" onClick={loadContent}>
                  <FileText className="size-3.5" aria-hidden /> {content ? 'Reload' : 'Show'} SKILL.md
                </button>
                {contentError ? <ErrorBox error={contentError} /> : null}
                {content && (
                  <div>
                    <p className="mb-1 text-[11px] text-faint">Untrusted content from the package, shown as plain text.</p>
                    {/* Plain text node on purpose: never markdown/HTML rendering of third-party content. */}
                    <pre className="max-h-96 overflow-auto whitespace-pre-wrap bg-void p-3 font-mono text-[11px]" data-testid="skill-content">
                      {content.text}
                    </pre>
                    {content.truncated && <p className="text-[11px] text-amber">Truncated.</p>}
                  </div>
                )}
              </>
            ) : (
              <p className="text-xs text-faint">Available after the skill is downloaded.</p>
            )}
          </Section>

          <Section title="History">
            {skill.events.length === 0 ? (
              <p className="text-xs text-faint">No events yet.</p>
            ) : (
              <ol className="flex flex-col gap-1 text-xs" aria-label="Skill events">
                {skill.events.map((e) => (
                  <li key={e.id} className="border-l border-line-strong pl-2" data-testid="skill-event">
                    <span className="text-faint">{formatDateTime(e.ts)}</span> <b>{e.action}</b>
                    {e.to_state && (
                      <span className="text-muted">
                        {' '}
                        {e.from_state ?? 'start'} → {e.to_state}
                      </span>
                    )}
                    {e.ref && <span className="font-mono text-faint"> @ {short(e.ref)}</span>}
                  </li>
                ))}
              </ol>
            )}
          </Section>
        </div>
      )}
      {confirm && (
        <Modal open onOpenChange={(o) => !o && setConfirm(null)} title={confirm.title}>
          <div className="flex flex-col gap-4 p-4 text-sm">
            {confirm.body}
            <div className="flex justify-end gap-2">
              <button className="btn" onClick={() => setConfirm(null)}>
                Cancel
              </button>
              <button className="btn btn-primary" disabled={busy} onClick={() => void run(confirm.action)}>
                {confirm.label}
              </button>
            </div>
          </div>
        </Modal>
      )}
    </Modal>
  );
}

function OutcomePanel({ n }: { n: { outcome: SkillOutcome; action: string } }) {
  const o = n.outcome;
  if (o.status === 'approval_required') {
    return (
      <div role="status" className="rounded-pixel border border-amber/60 bg-amber/10 p-3 text-xs" data-testid="approval-required">
        <div className="text-amber">Approval required. Nothing was activated.</div>
        <p className="text-ink">{o.message ?? 'An untrusted_tool_install approval is pending.'}</p>
        <p className="mt-1">
          <Link to="/approvals" className="text-signal underline" data-testid="approvals-link">
            Open Approvals
          </Link>{' '}
          to approve it, then come back and press Activate again.
        </p>
      </div>
    );
  }
  if (o.ok === false) {
    return (
      <div role="alert" className="rounded-pixel border border-danger/60 bg-danger/10 p-3 text-xs text-danger" data-testid="outcome-failed">
        {n.action} did not complete: {o.reason ?? 'unknown reason'}
        {o.state ? ` (state ${o.state})` : ''}
      </div>
    );
  }
  return (
    <div role="status" className="rounded-pixel border border-ok/50 bg-ok/10 p-3 text-xs text-ok" data-testid="outcome-ok">
      {o.changed === false ? `${n.action}: nothing changed${o.note ? ` (${o.note})` : ''}.` : `${n.action} done.`}
    </div>
  );
}
