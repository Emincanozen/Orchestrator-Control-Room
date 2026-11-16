import { RotateCcw, Save } from 'lucide-react';
import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { api } from '../api/endpoints';
import type { SettingsPayload } from '../api/types';
import { Async, ErrorBox, ViewHeader } from '../components/ui';
import { useResource } from '../lib/useResource';

interface LimitSpec {
  label: string;
  hint: string;
  unit?: string;
  /** zero is a meaningful value (mirrors _ZERO_OK_LIMITS in backend api/routes/system.py) */
  zeroOk?: boolean;
}

const LIMITS: Record<string, LimitSpec> = {
  max_parallel_workers: { label: 'Max parallel workers', hint: 'Tasks executing at the same time, globally. Machine RAM is the real ceiling.' },
  max_parallel_per_project: { label: 'Max parallel per project', hint: 'Concurrent tasks within one project.' },
  max_retry_attempts: { label: 'Max retry attempts', hint: 'Per task, across providers. 0 disables automatic retries.', zeroOk: true },
  max_repair_cycles: { label: 'Max repair cycles', hint: 'TESTING back to RUNNING loops before the task fails. 0 means no repair.', zeroOk: true },
  max_consecutive_failures: { label: 'Max consecutive failures', hint: 'Failures in a row before the task is failed.' },
  max_active_runtime_seconds: { label: 'Max run time', hint: 'Hard limit for one run.', unit: 's' },
  run_heartbeat_timeout_seconds: { label: 'Heartbeat timeout', hint: 'A run without a heartbeat for this long is treated as dead.', unit: 's' },
  max_consultations_per_task: { label: 'Max advisor consultations', hint: 'Per task. 0 disables the advisor.', zeroOk: true },
  max_leader_calls_per_task: { label: 'Max leader calls', hint: 'Plan / review calls per task.' },
  quota_recheck_seconds: { label: 'Quota recheck interval', hint: 'How often a quota-blocked task probes for resume.', unit: 's' },
  verify_timeout_seconds: { label: 'Verification timeout', hint: 'Per verification command.', unit: 's' },
  retry_backoff_seconds: { label: 'Retry backoff', hint: 'Wait before a retry. 0 retries immediately.', unit: 's', zeroOk: true },
};

/** Returns an error message or null. Mirrors the backend rule: numbers, >= 0, and > 0 unless zero is meaningful. */
export function validateLimit(key: string, raw: string): string | null {
  const text = raw.trim();
  if (text === '') return 'Required';
  const n = Number(text);
  if (!Number.isFinite(n)) return 'Must be a number';
  if (n < 0) return 'Must not be negative';
  if (n === 0 && !LIMITS[key]?.zeroOk) return 'Must be greater than zero';
  if (!Number.isInteger(n)) return 'Must be a whole number';
  return null;
}

function SettingsForm({ settings, onSaved }: { settings: SettingsPayload; onSaved: (s: SettingsPayload) => void }) {
  const effective = (settings.effective.limits ?? {}) as Record<string, number>;
  const defaults = (settings.defaults.limits ?? {}) as Record<string, number>;
  const overrides = (settings.overrides.limits ?? {}) as Record<string, number>;
  const anyOverride = Object.keys(overrides).some((k) => overrides[k] !== defaults[k]);
  const keys = useMemo(() => Object.keys(effective), [effective]);
  const [draft, setDraft] = useState<Record<string, string>>(() => Object.fromEntries(keys.map((k) => [k, String(effective[k])])));
  const [error, setError] = useState<unknown>(null);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setDraft(Object.fromEntries(Object.keys(effective).map((k) => [k, String(effective[k])])));
  }, [effective]);

  const errors = Object.fromEntries(keys.map((k) => [k, validateLimit(k, draft[k] ?? '')]));
  const changed = keys.filter((k) => Number(draft[k]) !== Number(effective[k]));
  const invalid = keys.some((k) => errors[k]);
  const perProjectWarning = Number(draft.max_parallel_per_project) > Number(draft.max_parallel_workers);

  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (invalid || changed.length === 0) return;
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      const res = await api.putSettings({ limits: Object.fromEntries(changed.map((k) => [k, Number(draft[k])])) });
      onSaved(res);
      setSaved(true);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const resetAll = async () => {
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      // Writing the defaults back is the only working reset: the backend's PUT {limits: null} (meant to
      // delete the override section) is rejected by its own limits validation (api/routes/system.py).
      onSaved(await api.putSettings({ limits: Object.fromEntries(Object.keys(overrides).map((k) => [k, defaults[k]])) }));
      setSaved(true);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={save} className="flex max-w-3xl flex-col gap-4 px-5 pb-8" aria-label="Policy limits">
      <p className="text-xs text-muted">
        Bounded-autonomy limits. Defaults come from <span className="font-mono">config/policies.yaml</span>; values you save here are stored as overrides and apply to the next scheduling decision.
      </p>
      <div className="grid gap-x-6 gap-y-4 md:grid-cols-2">
        {keys.map((k) => {
          const spec = LIMITS[k] ?? { label: k, hint: '' };
          const isOverridden = k in overrides && overrides[k] !== defaults[k];
          return (
            <div key={k} className="flex flex-col gap-1">
              <label htmlFor={`limit-${k}`} className="label flex items-center gap-2">
                {spec.label}
                {isOverridden && <span className="chip text-amber">overridden</span>}
              </label>
              <div className="flex items-center gap-2">
                <input
                  id={`limit-${k}`}
                  className="field w-32 font-mono"
                  inputMode="numeric"
                  value={draft[k] ?? ''}
                  onChange={(e) => {
                    setSaved(false);
                    setDraft((d) => ({ ...d, [k]: e.target.value }));
                  }}
                  aria-invalid={!!errors[k]}
                  aria-describedby={`limit-${k}-msg`}
                />
                {spec.unit && <span className="text-xs text-faint">{spec.unit}</span>}
                <span className="text-[11px] text-faint">default {String(defaults[k] ?? 'n/a')}</span>
              </div>
              <span id={`limit-${k}-msg`} className={`text-[11px] ${errors[k] ? 'text-danger' : 'text-faint'}`}>
                {errors[k] ?? spec.hint}
              </span>
            </div>
          );
        })}
      </div>
      {perProjectWarning && <p className="text-xs text-amber">Per-project parallelism above the global worker cap has no effect.</p>}
      {error ? <ErrorBox error={error} /> : null}
      {saved && (
        <p role="status" className="text-xs text-ok">
          Saved. The backend now applies these limits.
        </p>
      )}
      <div className="flex gap-2">
        <button type="submit" className="btn btn-primary" disabled={busy || invalid || changed.length === 0}>
          <Save className="size-3.5" aria-hidden /> Save limits
        </button>
        <button type="button" className="btn" onClick={() => void resetAll()} disabled={busy || !anyOverride}>
          <RotateCcw className="size-3.5" aria-hidden /> Reset to defaults
        </button>
      </div>
    </form>
  );
}

export function SettingsView() {
  const res = useResource(() => api.getSettings(), []);
  const [override, setOverride] = useState<SettingsPayload | null>(null);
  const current = override ?? res.data;
  return (
    <div>
      <ViewHeader title="Settings" subtitle="Policy limits applied by the scheduler and supervisor" />
      <Async res={{ ...res, data: current }} what="Settings">
        {(s) => <SettingsForm settings={s} onSaved={setOverride} />}
      </Async>
    </div>
  );
}
