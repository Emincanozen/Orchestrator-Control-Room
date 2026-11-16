import { useEffect, useState } from 'react';
import { api } from '../../api/endpoints';
import { ADVISOR_POLICIES, FALLBACK_POLICIES, type AdvisorPolicy, type FallbackPolicy, type Leadership, type LeadershipSettings } from '../../api/types';
import { ErrorBox, Section } from '../../components/ui';
import { CandidatePicker, candKey } from './CandidatePicker';

const ADVISOR_POLICY_TEXT: Record<AdvisorPolicy, string> = {
  never: 'Never: the advisor is not consulted, not even on request.',
  on_request: 'On request: only when you (or the leader explicitly) ask for it.',
  auto: 'Auto: also consulted automatically after repeated failed repair cycles.',
};
const FALLBACK_TEXT: Record<FallbackPolicy, string> = {
  off: 'Off: if the leader becomes unavailable (quota exhausted / auth error) nothing switches automatically.',
  same_provider_lower_tier: 'Same provider, lower tier: fall back to another model of the same provider.',
  any_capable: 'Any capable model: fall back to the best eligible model of any provider.',
};

/** Advisor identity + activation policy. */
export function AdvisorPanel({ l, onChanged }: { l: Leadership; onChanged: () => void }) {
  const current = l.advisor ? candKey(l.advisor) : '';
  const [pick, setPick] = useState(current);
  const [policy, setPolicy] = useState<AdvisorPolicy>(l.settings.advisor_policy);
  const [after, setAfter] = useState(l.settings.advisor_after_failed_repairs);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    setPick(l.advisor ? candKey(l.advisor) : '');
    setPolicy(l.settings.advisor_policy);
    setAfter(l.settings.advisor_after_failed_repairs);
  }, [l.advisor, l.settings.advisor_policy, l.settings.advisor_after_failed_repairs]);

  const cands = l.candidates.advisor;
  const chosen = cands.find((c) => candKey(c) === pick) ?? null;
  const dirty = pick !== current || policy !== l.settings.advisor_policy || after !== l.settings.advisor_after_failed_repairs;

  const save = async () => {
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      const body: Parameters<typeof api.putLeadership>[0] = { advisor_policy: policy, advisor_after_failed_repairs: after };
      if (pick !== current) body.advisor = chosen ? { provider: chosen.provider, model: chosen.model } : null;
      await api.putLeadership(body);
      setSaved(true);
      onChanged();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Section title="Advisor" aria="Advisor">
      <div className="text-sm">
        <span className="label mr-2">Current</span>
        <span className="font-mono" data-testid="current-advisor">
          {l.advisor ? `${l.advisor.provider} / ${l.advisor.model ?? 'default'}` : l.advisor_effective === 'router' ? 'chosen by the router per consult' : 'none (policy: never)'}
        </span>
      </div>
      <div className="grid gap-3 md:grid-cols-2">
        <div className="flex flex-col gap-1">
          <label htmlFor="advisor-pick" className="label">
            Advisor model
          </label>
          <CandidatePicker id="advisor-pick" label="Advisor model" candidates={cands} value={pick} onChange={setPick} emptyLabel="Let the router choose" />
          <span className="text-[11px] text-faint">
            An advisor from a different provider than the leader gives a more independent second opinion. Minimum tier: {l.role_minimum_tier.advisor ?? 'unknown'}.
          </span>
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="advisor-policy" className="label">
            Activation policy
          </label>
          <select id="advisor-policy" className="field" value={policy} onChange={(e) => setPolicy(e.target.value as AdvisorPolicy)}>
            {ADVISOR_POLICIES.map((p) => (
              <option key={p} value={p}>
                {p.replace('_', ' ')}
              </option>
            ))}
          </select>
          <span className="text-[11px] text-faint">{ADVISOR_POLICY_TEXT[policy]}</span>
          {policy === 'auto' && (
            <label className="mt-1 flex items-center gap-2 text-xs text-muted">
              Consult after
              <input
                type="number"
                min={1}
                max={20}
                className="field w-16"
                aria-label="Failed repair cycles before an automatic consult"
                value={after}
                onChange={(e) => setAfter(Number(e.target.value))}
              />
              failed repair cycles
            </label>
          )}
        </div>
      </div>
      {error ? <ErrorBox error={error} /> : null}
      <div className="flex items-center gap-3">
        <button className="btn btn-primary" disabled={!dirty || busy || after < 1 || after > 20} onClick={save}>
          Save advisor settings
        </button>
        {saved && !dirty ? <span className="text-xs text-ok">saved</span> : null}
      </div>
    </Section>
  );
}

type BudgetKey = 'max_consultations_per_day' | 'max_consultations_per_task' | 'max_leader_calls_per_task' | 'max_parallel_workers';
const BUDGETS: { key: BudgetKey; label: string; min: number; max: number; used?: (l: Leadership) => number | null; hint: string }[] = [
  { key: 'max_consultations_per_day', label: 'Consultations per day', min: 0, max: 1000, used: (l) => l.budgets.consultations_today.used, hint: 'advisor consults started today' },
  { key: 'max_consultations_per_task', label: 'Consultations per task', min: 0, max: 50, hint: 'per-task usage is checked by the backend when a consult is requested' },
  { key: 'max_leader_calls_per_task', label: 'Leader calls per task', min: 1, max: 100, hint: 'planning / review calls the leader may make for one task' },
  { key: 'max_parallel_workers', label: 'Parallel workers', min: 1, max: 16, hint: 'tasks running at once' },
];

/** Fallback policy + budgets (consultations / leader calls / parallel workers). */
export function BudgetsPanel({ l, onChanged }: { l: Leadership; onChanged: () => void }) {
  const [fallback, setFallback] = useState<FallbackPolicy>(l.settings.fallback_policy);
  const [vals, setVals] = useState<Record<BudgetKey, number>>(pickBudgets(l.settings));
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setFallback(l.settings.fallback_policy);
    setVals(pickBudgets(l.settings));
  }, [l.settings]);

  const invalid = BUDGETS.some((b) => !Number.isInteger(vals[b.key]) || vals[b.key] < b.min || vals[b.key] > b.max);
  const dirty = fallback !== l.settings.fallback_policy || BUDGETS.some((b) => vals[b.key] !== l.settings[b.key]);
  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.putLeadership({ fallback_policy: fallback, ...vals });
      onChanged();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Section title="Fallback & budgets" aria="Fallback and budgets">
      <div className="flex flex-col gap-1">
        <label htmlFor="fallback-policy" className="label">
          Automatic leader fallback
        </label>
        <select id="fallback-policy" className="field max-w-md" value={fallback} onChange={(e) => setFallback(e.target.value as FallbackPolicy)}>
          {FALLBACK_POLICIES.map((p) => (
            <option key={p} value={p}>
              {p.replace(/_/g, ' ')}
            </option>
          ))}
        </select>
        <span className="text-[11px] text-faint">{FALLBACK_TEXT[fallback]} A fallback uses the same safe switch (checkpoint, validation, rollback).</span>
      </div>
      <div className="grid gap-3 md:grid-cols-2">
        {BUDGETS.map((b) => {
          const used = b.used?.(l);
          const bad = !Number.isInteger(vals[b.key]) || vals[b.key] < b.min || vals[b.key] > b.max;
          return (
            <div key={b.key} className="flex flex-col gap-1" data-testid={`budget-${b.key}`}>
              <label htmlFor={`b-${b.key}`} className="label">
                {b.label}
              </label>
              <div className="flex items-center gap-2">
                <input
                  id={`b-${b.key}`}
                  type="number"
                  min={b.min}
                  max={b.max}
                  className="field w-24"
                  aria-invalid={bad}
                  value={Number.isNaN(vals[b.key]) ? '' : vals[b.key]}
                  onChange={(e) => setVals({ ...vals, [b.key]: e.target.value === '' ? NaN : Number(e.target.value) })}
                />
                <span className="tick-num text-xs text-muted" data-testid={`used-${b.key}`}>
                  {used !== undefined && used !== null ? `used ${used} / limit ${l.settings[b.key]}` : `limit ${l.settings[b.key]}`}
                </span>
              </div>
              <span className={`text-[11px] ${bad ? 'text-danger' : 'text-faint'}`}>{bad ? `Whole number from ${b.min} to ${b.max}.` : b.hint}</span>
            </div>
          );
        })}
      </div>
      {error ? <ErrorBox error={error} /> : null}
      <div>
        <button className="btn btn-primary" disabled={!dirty || invalid || busy} onClick={save}>
          Save fallback &amp; budgets
        </button>
      </div>
    </Section>
  );
}

function pickBudgets(s: LeadershipSettings): Record<BudgetKey, number> {
  return {
    max_consultations_per_day: s.max_consultations_per_day,
    max_consultations_per_task: s.max_consultations_per_task,
    max_leader_calls_per_task: s.max_leader_calls_per_task,
    max_parallel_workers: s.max_parallel_workers,
  };
}

/** Read-only: the backend exposes no endpoint to change approval policy from here. */
export function PermissionsPanel({ l }: { l: Leadership }) {
  const p = l.permissions;
  return (
    <Section title="Permissions & approvals" aria="Permissions and approvals">
      <p className="text-xs text-muted">Read-only. These come from the policy files; this dashboard cannot change them.</p>
      <div className="grid gap-3 md:grid-cols-2">
        <div>
          <div className="label mb-1">Requires your approval</div>
          {p.approvals_required_for.length ? (
            <ul className="flex flex-wrap gap-1.5" aria-label="Requires approval">
              {p.approvals_required_for.map((a) => (
                <li key={a} className="chip text-amber">
                  {a}
                </li>
              ))}
            </ul>
          ) : (
            <span className="text-xs text-faint">nothing listed</span>
          )}
        </div>
        <div>
          <div className="label mb-1">Auto-approved</div>
          {p.auto_approve.length ? (
            <ul className="flex flex-wrap gap-1.5" aria-label="Auto-approved">
              {p.auto_approve.map((a) => (
                <li key={a} className="chip text-ok">
                  {a}
                </li>
              ))}
            </ul>
          ) : (
            <span className="text-xs text-faint">nothing is auto-approved</span>
          )}
        </div>
      </div>
      <WorkerPermissions wp={p.worker_permissions} />
    </Section>
  );
}

function WorkerPermissions({ wp }: { wp: Record<string, unknown> | undefined }) {
  const entries = Object.entries(wp ?? {});
  if (!entries.length) return null;
  return (
    <div>
      <div className="label mb-1">Worker permissions</div>
      <ul className="flex flex-wrap gap-1.5" aria-label="Worker permissions">
        {entries.map(([k, v]) => (
          <li key={k} className="chip" data-testid={`worker-permission-${k}`}>
            {k}: {String(v)}
            {k === 'claude_shell' && <span className="ml-1 text-amber">(config/policies.yaml only, read-only here)</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}
