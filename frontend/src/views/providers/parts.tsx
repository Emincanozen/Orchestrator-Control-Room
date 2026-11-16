import { useMemo, useState } from 'react';
import type { ProviderModel, ProviderStatus, ProviderUsageState, UsageEntry, UsageValue } from '../../api/types';
import { ExactnessChip } from '../../components/ui';
import { CAPABILITY_FLAGS, EXACTNESS_TEXT, USAGE_STATE_TEXT, billingInfo, countdown } from '../../lib/explain';
import { formatDateTime, parseTs, relativeAgo, useNow } from '../../lib/time';

export const UNKNOWN_USAGE_TEXT = 'Unknown — the provider does not expose this';

export function UsageStateBadge({ state }: { state: string }) {
  const meta = USAGE_STATE_TEXT[state as keyof typeof USAGE_STATE_TEXT];
  return (
    <span className="chip" style={{ color: meta?.color ?? 'var(--color-muted)' }} data-usage-state={state} title={meta?.text}>
      <span aria-hidden className="inline-block size-1.5" style={{ background: 'currentColor' }} />
      {meta?.label ?? state}
    </span>
  );
}

/** Ticks locally every second from the recorded reset timestamp. */
export function ResetCountdown({ iso, prefix = 'resets in' }: { iso: string | null | undefined; prefix?: string }) {
  const now = useNow();
  const t = parseTs(iso);
  if (t === null) return null;
  const text = countdown(t, now);
  return (
    <span className="tick-num text-xs text-muted" title={formatDateTime(iso)} data-testid="reset-countdown">
      {text === 'reset time reached' ? text : `${prefix} ${text}`}
    </span>
  );
}

export function UsageStateBlock({ usage }: { usage: ProviderUsageState | null }) {
  if (!usage) return <p className="text-xs text-faint">{UNKNOWN_USAGE_TEXT}.</p>;
  return (
    <div className="flex flex-col gap-1" data-testid="usage-state-block">
      <div className="flex flex-wrap items-center gap-2">
        <UsageStateBadge state={usage.state} />
        <ResetCountdown iso={usage.resets_at} />
        {usage.until && !usage.resets_at && <ResetCountdown iso={usage.until} prefix="rechecks in" />}
        {usage.confidence && <span className="text-[11px] text-faint">confidence: {usage.confidence}</span>}
      </div>
      {usage.state === 'UNKNOWN' && !usage.reason ? <p className="text-xs text-muted">{UNKNOWN_USAGE_TEXT}.</p> : null}
      {usage.reason && <p className="text-xs text-muted">{usage.reason}</p>}
      {usage.evidence_metric && <p className="text-[11px] text-faint">evidence: {usage.evidence_metric}</p>}
    </div>
  );
}

export function AuthText({ p }: { p: ProviderStatus }) {
  const text = p.authenticated === true ? 'Yes' : p.authenticated === false ? 'No' : 'Cannot be determined';
  const color = p.authenticated === true ? 'text-ok' : p.authenticated === false ? 'text-danger' : 'text-amber';
  return (
    <span data-testid={`auth-${p.provider}`}>
      <span className={color}>{text}</span>
      {p.auth_detail ? <span className="text-muted"> — {p.auth_detail}</span> : null}
    </span>
  );
}

export function BillingBadge({ cls }: { cls: string | null | undefined }) {
  const b = billingInfo(cls);
  return (
    <span className="chip text-signal" title={b.text} data-billing={cls ?? 'unknown'}>
      {b.label}
    </span>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[7.5rem_1fr] gap-2 py-0.5 text-xs">
      <dt className="label pt-0.5">{label}</dt>
      <dd className="min-w-0 break-words text-ink">{children}</dd>
    </div>
  );
}

export function ProviderCard({ p }: { p: ProviderStatus }) {
  const caps = p.capabilities;
  const billing = billingInfo(p.billing_class);
  return (
    <article className="pixel-card flex flex-col gap-3 p-4" aria-label={`Provider ${p.provider}`} data-testid={`provider-${p.provider}`}>
      <header className="flex items-center justify-between gap-2">
        <h3 className="display text-sm">{p.provider}</h3>
        <span className="chip" style={{ color: p.installed ? 'var(--color-ok)' : 'var(--color-danger)' }}>
          {p.installed ? 'installed' : p.detected ? 'not installed' : 'detection pending'}
        </span>
      </header>
      <dl>
        <Row label="Version">{p.version ? <span className="font-mono">{p.version}</span> : <span className="text-faint">not reported</span>}</Row>
        <Row label="Executable">{p.executable ? <span className="font-mono">{p.executable}</span> : <span className="text-faint">not found</span>}</Row>
        <Row label="Authenticated">
          <AuthText p={p} />
        </Row>
        <Row label="Billing">
          <BillingBadge cls={p.billing_class} />
          <span className="mt-1 block text-muted">{billing.text}</span>
        </Row>
        <Row label="Models">{p.model_count} discovered</Row>
        <Row label="Checked">{p.checked_at ? formatDateTime(p.checked_at) : <span className="text-faint">never</span>}</Row>
      </dl>
      {p.error && (
        <p role="note" className="rounded-pixel border border-danger/50 bg-danger/10 px-2 py-1 text-xs text-danger">
          {p.error}
        </p>
      )}
      <section aria-label={`Usage ${p.provider}`}>
        <h4 className="label mb-1">Usage</h4>
        <UsageStateBlock usage={p.usage} />
      </section>
      <section aria-label={`Capabilities ${p.provider}`}>
        <h4 className="label mb-1">Capabilities</h4>
        {caps ? (
          <>
            <ul className="grid grid-cols-1 gap-y-0.5 text-xs">
              {CAPABILITY_FLAGS.map((f) => {
                const on = Boolean((caps as unknown as Record<string, unknown>)[f.key]);
                return (
                  <li key={f.key} title={f.hint} className="flex items-center justify-between gap-2" data-cap={f.key} data-supported={on}>
                    <span className={on ? 'text-ink' : 'text-faint'}>{f.label}</span>
                    <span className={on ? 'text-ok' : 'text-faint'}>{on ? 'supported' : 'not supported'}</span>
                  </li>
                );
              })}
            </ul>
            <div className="mt-2 text-xs">
              <span className="text-muted">Sandbox modes: </span>
              {caps.sandbox_modes.length ? caps.sandbox_modes.join(', ') : <span className="text-faint">none declared</span>}
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-1 text-xs">
              <span className="text-muted">Task capabilities: </span>
              {caps.task_capabilities.length ? (
                caps.task_capabilities.map((c) => (
                  <span key={c} className="chip text-muted">
                    {c}
                  </span>
                ))
              ) : (
                <span className="text-faint">none declared</span>
              )}
            </div>
          </>
        ) : (
          <p className="text-xs text-faint">Capabilities are unknown until detection has run.</p>
        )}
      </section>
    </article>
  );
}

function ValueCell({ v }: { v: UsageValue }) {
  const unknown = v.exactness === 'unknown' || (v.value === null && v.exactness !== 'exact');
  const markerOnly = v.value === null && !unknown; // an observation without a numeric value (e.g. a status marker)
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      {markerOnly ? (
        <span className="text-muted" data-testid="usage-marker">
          Recorded, no numeric value
        </span>
      ) : unknown ? (
        <span className="text-faint" data-testid="usage-unknown">
          {UNKNOWN_USAGE_TEXT}
        </span>
      ) : (
        <span className="tick-num" data-testid="usage-number">
          {v.value}
          {v.unit ? ` ${v.unit}` : ''}
        </span>
      )}
      <span title={EXACTNESS_TEXT[v.exactness]}>
        <ExactnessChip exactness={v.exactness} />
      </span>
    </span>
  );
}

export function UsageEntryCard({ e }: { e: UsageEntry }) {
  const now = useNow();
  return (
    <section className="pixel-card p-3" aria-label={`Usage ${e.provider}${e.model ? ` ${e.model}` : ''}`} data-testid="usage-entry">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className="display text-[13px]">{e.provider}</span>
        <span className="font-mono text-xs text-muted">{e.model ?? 'provider-wide'}</span>
        <UsageStateBadge state={e.usage_state.state} />
        <ResetCountdown iso={e.usage_state.resets_at} />
        {e.usage_state.reason && <span className="text-xs text-muted">{e.usage_state.reason}</span>}
      </div>
      {e.values.length === 0 ? (
        <p className="text-xs text-faint">{UNKNOWN_USAGE_TEXT}. Nothing has been observed yet.</p>
      ) : (
        <table className="w-full text-left text-xs">
          <thead className="label">
            <tr>
              {['Metric', 'Value', 'Source', 'Observed', 'Resets'].map((h) => (
                <th key={h} className="px-2 py-1 font-normal">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {e.values.map((v, i) => (
              <tr key={`${v.metric}-${i}`} className="border-t border-line/60">
                <td className="px-2 py-1.5 font-mono">{v.metric}</td>
                <td className="px-2 py-1.5">
                  <ValueCell v={v} />
                </td>
                <td className="px-2 py-1.5 text-muted">{v.source}</td>
                <td className="px-2 py-1.5 text-muted" title={formatDateTime(v.observed_at)}>
                  {relativeAgo(v.observed_at, now)}
                </td>
                <td className="px-2 py-1.5 text-muted">{v.resets_at ? formatDateTime(v.resets_at) : '--'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

const VERIFICATION_TEXT: Record<string, string> = {
  verified: 'Exercised by a real run on this machine.',
  listed: 'Offered by the CLI but never exercised here.',
  unlisted: 'Discovered at runtime; not in the curated registry.',
};

export function ModelsTable({ rows }: { rows: ProviderModel[] }) {
  const [provider, setProvider] = useState('');
  const [tier, setTier] = useState('');
  const [capability, setCapability] = useState('');
  const providers = useMemo(() => [...new Set(rows.map((r) => r.provider))].sort(), [rows]);
  const tiers = useMemo(() => [...new Set(rows.map((r) => r.tier).filter((t): t is string => !!t))], [rows]);
  const caps = useMemo(() => [...new Set(rows.flatMap((r) => [...r.capabilities, ...r.listed_capabilities]))].sort(), [rows]);
  const shown = rows.filter(
    (r) =>
      (!provider || r.provider === provider) &&
      (!tier || r.tier === tier) &&
      (!capability || r.capabilities.includes(capability) || r.listed_capabilities.includes(capability)),
  );
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap gap-2" role="group" aria-label="Model filters">
        <select className="field w-auto" aria-label="Filter by provider" value={provider} onChange={(e) => setProvider(e.target.value)}>
          <option value="">All providers</option>
          {providers.map((p) => (
            <option key={p}>{p}</option>
          ))}
        </select>
        <select className="field w-auto" aria-label="Filter by tier" value={tier} onChange={(e) => setTier(e.target.value)}>
          <option value="">All tiers</option>
          {tiers.map((t) => (
            <option key={t}>{t}</option>
          ))}
        </select>
        <select className="field w-auto" aria-label="Filter by capability" value={capability} onChange={(e) => setCapability(e.target.value)}>
          <option value="">Any capability</option>
          {caps.map((c) => (
            <option key={c}>{c}</option>
          ))}
        </select>
        <span className="self-center text-xs text-faint" role="status">
          {shown.length} of {rows.length} models
        </span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs" aria-label="Models">
          <thead className="label">
            <tr>
              {['Provider', 'Model', 'Tier', 'Verification', 'Capabilities', 'Billing', 'Discovered'].map((h) => (
                <th key={h} className="px-2 py-1 font-normal">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {shown.map((m) => {
              const listedOnly = m.listed_capabilities.filter((c) => !m.capabilities.includes(c));
              return (
                <tr key={`${m.provider}/${m.model_id}`} className="border-t border-line/60 align-top" data-testid="model-row">
                  <td className="px-2 py-1.5">{m.provider}</td>
                  <td className="px-2 py-1.5">
                    <div className="font-mono">{m.model_id}</div>
                    {(m.alias || m.display_name) && <div className="text-[11px] text-faint">{[m.alias, m.display_name].filter(Boolean).join(' · ')}</div>}
                  </td>
                  <td className="px-2 py-1.5">
                    <div>{m.tier ?? 'unknown'}</div>
                    <div className="text-[11px] text-faint" title={m.tier_source === 'default' ? 'No configured tier: the default was applied.' : 'Tier set in the model config.'}>
                      {m.tier_source === 'default' ? 'default (not configured)' : m.tier_source === 'config' ? 'from config' : (m.tier_source ?? 'source unknown')}
                    </div>
                  </td>
                  <td className="px-2 py-1.5">
                    <span className="chip" style={{ color: m.verification === 'verified' ? 'var(--color-ok)' : 'var(--color-amber)' }} title={m.verification ? VERIFICATION_TEXT[m.verification] : undefined}>
                      {m.verification ?? 'unknown'}
                    </span>
                  </td>
                  <td className="px-2 py-1.5">
                    <div className="flex flex-wrap gap-1">
                      {m.capabilities.map((c) => (
                        <span key={c} className="chip text-ink" title="Verified capability">
                          {c}
                        </span>
                      ))}
                      {listedOnly.map((c) => (
                        <span key={c} className="chip text-amber" title="Listed by the CLI but never verified" data-listed-only="true">
                          {c} (listed, unverified)
                        </span>
                      ))}
                      {m.capabilities.length === 0 && listedOnly.length === 0 && <span className="text-faint">none</span>}
                    </div>
                  </td>
                  <td className="px-2 py-1.5">
                    <span className="text-muted">{billingInfo(m.billing_class).label}</span>
                  </td>
                  <td className="px-2 py-1.5">
                    {m.discovered ? <span className="text-ok">discovered</span> : <span className="text-faint" title="Configured, but the CLI did not report it">not discovered</span>}
                    {!m.provider_installed && <div className="text-[11px] text-danger">provider not installed</div>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
