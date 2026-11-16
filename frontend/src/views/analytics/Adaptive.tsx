import { useEffect, useState } from 'react';
import { api } from '../../api/endpoints';
import type { AnalyticsRouting } from '../../api/types';
import { ErrorBox, Section } from '../../components/ui';
import { NO_DATA } from '../../lib/analytics';
import { formatDateTime } from '../../lib/time';
import { AdaptiveCells, ProviderDot } from './Tables';

export function AdaptivePanel({ routing, onSaved }: { routing: AnalyticsRouting; onSaved: () => void }) {
  const s = routing.settings;
  const [enabled, setEnabled] = useState(s.enabled);
  const [minSamples, setMinSamples] = useState<string>(String(s.min_samples));
  const [maxAdj, setMaxAdj] = useState<string>(String(s.max_adjustment));
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setEnabled(s.enabled);
    setMinSamples(String(s.min_samples));
    setMaxAdj(String(s.max_adjustment));
  }, [s.enabled, s.min_samples, s.max_adjustment]);

  const [msLo, msHi] = s.bounds.min_samples;
  const [maLo, maHi] = s.bounds.max_adjustment;
  const ms = Number(minSamples);
  const ma = Number(maxAdj);
  const msErr = minSamples.trim() === '' || !Number.isInteger(ms) || ms < msLo || ms > msHi ? `Whole number from ${msLo} to ${msHi}.` : null;
  const maErr = maxAdj.trim() === '' || !Number.isFinite(ma) || ma < maLo || ma > maHi ? `Number from ${maLo} to ${maHi}.` : null;
  const dirty = enabled !== s.enabled || ms !== s.min_samples || ma !== s.max_adjustment;

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.putAnalyticsAdaptiveSettings({ enabled, min_samples: ms, max_adjustment: ma });
      onSaved();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Section title="Adaptive routing" aria="Adaptive routing">
      <p className="text-xs text-muted">
        When enabled, the router nudges a model&apos;s score by the measured success rate of its verified runs, but only after the sample threshold is met and never by more than the cap. {s.note}
      </p>
      <div className="grid gap-4 md:grid-cols-3">
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} aria-label="Adaptive routing enabled" />
          Enabled
        </label>
        <div className="flex flex-col gap-1">
          <label htmlFor="ad-min" className="label">
            Minimum verified runs
          </label>
          <input id="ad-min" type="number" className="field w-28" min={msLo} max={msHi} value={minSamples} aria-invalid={!!msErr} onChange={(e) => setMinSamples(e.target.value)} />
          <span className={`text-[11px] ${msErr ? 'text-danger' : 'text-faint'}`}>{msErr ?? `${msLo} to ${msHi}`}</span>
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="ad-max" className="label">
            Maximum adjustment (points)
          </label>
          <input id="ad-max" type="number" step="0.5" className="field w-28" min={maLo} max={maHi} value={maxAdj} aria-invalid={!!maErr} onChange={(e) => setMaxAdj(e.target.value)} />
          <span className={`text-[11px] ${maErr ? 'text-danger' : 'text-faint'}`}>{maErr ?? `${maLo} to ${maHi}; effective cap now ${s.effective_cap_points} pts`}</span>
        </div>
      </div>
      {error ? <ErrorBox error={error} /> : null}
      <div>
        <button className="btn btn-primary" disabled={!dirty || !!msErr || !!maErr || busy} onClick={save}>
          Save adaptive settings
        </button>
      </div>
      <AdaptiveCells cells={routing.cells} />
      <div>
        <div className="label mb-1">Decisions where adaptive routing was applied ({routing.decisions_with_adaptive})</div>
        {routing.applied_history.length === 0 ? (
          <p className="text-sm text-faint" data-testid="no-adaptive-history">
            {NO_DATA}: no routing decision has used an adaptive adjustment yet.
          </p>
        ) : (
          <ul className="max-h-60 overflow-auto text-[13px]" aria-label="Adaptive decision history">
            {routing.applied_history.map((h) => (
              <li key={h.id} className="flex flex-wrap items-baseline gap-x-3 border-b border-line/50 py-1" data-testid="adaptive-history">
                <span className="text-xs text-faint">{formatDateTime(h.created_at)}</span>
                <span className="font-mono text-xs">
                  <ProviderDot provider={h.provider} />
                  {h.provider} / {h.model ?? 'default'}
                </span>
                <span className="text-xs text-muted">{h.phase}</span>
                <a className="text-xs text-signal underline" href={`#/routing?task=${encodeURIComponent(h.task_id)}`}>
                  routing decision
                </a>
                <span className="basis-full text-[11px] text-muted">{h.adaptive_reasons.join('; ')}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Section>
  );
}
