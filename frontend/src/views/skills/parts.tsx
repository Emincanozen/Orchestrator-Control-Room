import { Check, Copy, ExternalLink, Star } from 'lucide-react';
import { useState } from 'react';
import type { Project, Skill, SkillState } from '../../api/types';

export const STATE_META: Record<SkillState, { label: string; color: string; text: string }> = {
  CATALOGUED_ON_DEMAND: { label: 'Catalogued', color: 'var(--color-muted)', text: 'Listed only. Nothing has been downloaded.' },
  DOWNLOADED_AND_DISABLED: { label: 'Downloaded', color: 'var(--color-signal)', text: 'Files are downloaded but not active in any project.' },
  INSTALLED_AND_ACTIVE: { label: 'Active', color: 'var(--color-ok)', text: 'Active in at least one project.' },
  INSTALL_FAILED: { label: 'Install failed', color: 'var(--color-danger)', text: 'The last download failed. It can be retried.' },
  INCOMPATIBLE: { label: 'Incompatible', color: 'var(--color-danger)', text: 'Not compatible with this machine; see the reasons.' },
  NEEDS_SECURITY_REVIEW: { label: 'Needs security review', color: 'var(--color-amber)', text: 'Downloaded, but the inspection found something to review before activation.' },
};

export const KIND_LABEL: Record<string, string> = {
  agent_skill: 'agent skill',
  mcp_server: 'MCP server',
  standalone_app: 'standalone app',
  dev_tool: 'dev tool',
  collection: 'collection',
  external_service: 'external service',
  docs: 'docs',
};

export const CATALOGUE_ONLY_KINDS = ['standalone_app', 'dev_tool', 'external_service', 'docs'];

export const DOWNLOADED_STATES: SkillState[] = ['DOWNLOADED_AND_DISABLED', 'NEEDS_SECURITY_REVIEW', 'INSTALLED_AND_ACTIVE'];
export const INSTALLABLE_FROM: SkillState[] = ['CATALOGUED_ON_DEMAND', 'INSTALL_FAILED', 'INCOMPATIBLE'];

export function SkillStateBadge({ state }: { state: string }) {
  const m = STATE_META[state as SkillState];
  return (
    <span className="chip" style={{ color: m?.color ?? 'var(--color-muted)' }} data-skill-state={state} title={m?.text}>
      <span aria-hidden className="inline-block size-1.5" style={{ background: 'currentColor' }} />
      {m?.label ?? state}
    </span>
  );
}

const COMPAT_LABEL: Record<string, string> = { claude_code: 'Claude Code', codex: 'Codex', mcp: 'MCP' };

export function CompatChips({ compat }: { compat: Record<string, unknown> }) {
  const entries = Object.entries(COMPAT_LABEL).filter(([k]) => compat[k] !== undefined && compat[k] !== 'n/a');
  if (!entries.length) return null;
  return (
    <span className="flex flex-wrap gap-1" aria-label="Compatibility">
      {entries.map(([k, label]) => {
        const v = String(compat[k]);
        const color = v === 'yes' || v === 'true' ? 'text-ok' : v === 'no' || v === 'false' ? 'text-danger' : 'text-faint';
        return (
          <span key={k} className={`chip ${color}`} title={`${label}: ${v}`}>
            {label}: {v}
          </span>
        );
      })}
    </span>
  );
}

export function RiskChip({ risk }: { risk: string | null }) {
  const color = risk === 'low' ? 'text-ok' : risk === 'medium' ? 'text-amber' : risk === 'high' ? 'text-danger' : 'text-faint';
  return <span className={`chip ${color}`}>risk: {risk ?? 'unknown'}</span>;
}

export function SourceMark({ skill }: { skill: Pick<Skill, 'source_verified' | 'has_uncertainty' | 'source_url' | 'source'> }) {
  if (!skill.source_url) return <span className="chip text-amber" title="No source URL is known for this entry">no source</span>;
  if (skill.source === 'user') return <span className="chip text-signal" title="Source URL set by you; not verified by the catalogue">your source</span>;
  if (!skill.source_verified || skill.has_uncertainty)
    return (
      <span className="chip text-amber" title="The catalogue marks this source as uncertain; open the details to see why">
        source uncertain
      </span>
    );
  return (
    <span className="chip text-ok" title="Source verified when the catalogue was researched">
      <Check className="size-3" aria-hidden /> verified source
    </span>
  );
}

export function CopyButton({ text, label }: { text: string; label: string }) {
  const [done, setDone] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setDone(true);
      setTimeout(() => setDone(false), 1500);
    } catch {
      setDone(false);
    }
  };
  return (
    <button type="button" className="btn btn-sm" onClick={copy} aria-label={label}>
      {done ? <Check className="size-3" aria-hidden /> : <Copy className="size-3" aria-hidden />} {done ? 'Copied' : 'Copy'}
    </button>
  );
}

/** Source URL as plain text with a copy button; opened only on an explicit click, without opener access. */
export function SourceLink({ url }: { url: string }) {
  const openable = /^https:\/\//i.test(url);
  return (
    <span className="flex flex-wrap items-center gap-2">
      <span className="break-all font-mono text-xs" data-testid="source-url">
        {url}
      </span>
      <CopyButton text={url} label="Copy source URL" />
      <button
        type="button"
        className="btn btn-sm"
        disabled={!openable}
        title={openable ? 'Opens the page in a new tab' : 'Only https links can be opened'}
        onClick={() => window.open(url, '_blank', 'noopener,noreferrer')}
      >
        <ExternalLink className="size-3" aria-hidden /> Open
      </button>
    </span>
  );
}

export function SkillCard({ s, onOpen, project }: { s: Skill; onOpen: () => void; project: Project | undefined }) {
  const conflicts = s.warnings.filter((w) => w.type === 'conflict');
  const dups = s.warnings.filter((w) => w.type === 'duplicate');
  return (
    <li className="pixel-card flex flex-col gap-2 p-3" data-testid="skill-card" data-skill-id={s.id}>
      <div className="flex items-start justify-between gap-2">
        <button className="min-w-0 text-left" onClick={onOpen} aria-label={`Open skill ${s.name}`}>
          <span className="block truncate text-[13px] font-semibold underline-offset-2 hover:underline">{s.name}</span>
          <span className="text-[11px] text-faint">{s.category ?? 'uncategorised'}</span>
        </button>
        <span className="chip text-signal">{KIND_LABEL[s.kind] ?? s.kind}</span>
      </div>
      <p className="line-clamp-3 text-xs text-muted">{s.description}</p>
      <div className="flex flex-wrap items-center gap-1.5">
        <SkillStateBadge state={s.state} />
        <SourceMark skill={s} />
        <RiskChip risk={s.risk} />
      </div>
      <CompatChips compat={s.compat} />
      {(conflicts.length > 0 || dups.length > 0) && (
        <ul className="flex flex-col gap-0.5 text-[11px] text-amber" aria-label="Warnings">
          {conflicts.map((w, i) => (
            <li key={`c${i}`}>Conflict: {w.reason} ({w.with.join(', ')})</li>
          ))}
          {dups.map((w, i) => (
            <li key={`d${i}`}>Possible duplicate of {w.with.join(', ')}</li>
          ))}
        </ul>
      )}
      <div className="mt-auto flex flex-wrap items-center gap-2 text-[11px] text-faint">
        {s.installed_ref && <span className="font-mono" title="Installed ref">installed {s.installed_ref.slice(0, 8)}</span>}
        {s.pinned_ref && <span className="font-mono" title="Pinned ref">pinned {s.pinned_ref.slice(0, 8)}</span>}
        {s.previous_ref && <span className="font-mono" title="Previous ref (rollback target)">previous {s.previous_ref.slice(0, 8)}</span>}
        {project && s.project_activation && (
          <span className={s.project_activation.active ? 'text-ok' : 'text-faint'} data-testid="project-activation">
            {s.project_activation.active ? `active in ${project.name}` : `not active in ${project.name}`}
          </span>
        )}
      </div>
    </li>
  );
}

export function PriorityMark() {
  return (
    <span className="chip text-amber" title="Marked as a priority candidate in the catalogue">
      <Star className="size-3" aria-hidden /> priority
    </span>
  );
}
