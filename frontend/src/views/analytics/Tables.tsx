import { Check, Minus } from 'lucide-react';
import { useMemo, useState } from 'react';
import type { AdaptiveCell, AgentStatsRow, Metric, ModelRow } from '../../api/types';
import { providerColor } from '../../design/tokens';
import { LOW_EVIDENCE_N, NO_DATA, formatSeconds, isMissing, num, pct } from '../../lib/analytics';
import { formatDateTime } from '../../lib/time';

export function ProviderDot({ provider }: { provider: string | null }) {
  return <span aria-hidden className="mr-1.5 inline-block size-2.5 align-middle" style={{ background: providerColor(provider) }} />;
}

type SortKey = 'provider' | 'model' | 'task_type' | 'runs' | 'verified_runs' | 'success_rate' | 'avg_duration_seconds' | 'avg_tokens' | 'last_used';

const COLS: { key: SortKey; label: string; right?: boolean }[] = [
  { key: 'provider', label: 'Provider' },
  { key: 'model', label: 'Model' },
  { key: 'task_type', label: 'Task type' },
  { key: 'runs', label: 'Runs', right: true },
  { key: 'verified_runs', label: 'Verified runs (n)', right: true },
  { key: 'success_rate', label: 'Success rate', right: true },
  { key: 'avg_duration_seconds', label: 'Avg duration', right: true },
  { key: 'avg_tokens', label: 'Avg tokens', right: true },
  { key: 'last_used', label: 'Last used' },
];

/** Sortable model performance table. `minSamples` is the adaptive-routing threshold used for the marker. */
export function ModelTable({ rows, minSamples }: { rows: ModelRow[]; minSamples: number | null }) {
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: 'verified_runs', dir: -1 });
  const sorted = useMemo(() => {
    const get = (r: ModelRow): string | number | null => {
      const v = r[sort.key] as unknown;
      return v !== null && typeof v === 'object' ? ((v as Metric).value as number | null) : (v as string | number | null);
    };
    return [...rows].sort((a, b) => {
      const x = get(a);
      const y = get(b);
      if (x === null && y === null) return 0;
      if (x === null) return 1; // missing values always last
      if (y === null) return -1;
      return (typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y))) * sort.dir;
    });
  }, [rows, sort]);
  if (rows.length === 0) return <p className="py-6 text-center text-sm text-faint">{NO_DATA}: no execute or repair run with a model yet.</p>;
  return (
    <div className="overflow-auto">
      <table className="w-full text-[13px]" aria-label="Model performance">
        <thead>
          <tr className="label text-left">
            {COLS.map((c) => (
              <th key={c.key} className={`py-1 pr-3 font-normal ${c.right ? 'text-right' : ''}`} aria-sort={sort.key === c.key ? (sort.dir === 1 ? 'ascending' : 'descending') : 'none'}>
                <button className="uppercase hover:text-ink" onClick={() => setSort((s) => ({ key: c.key, dir: s.key === c.key ? ((-s.dir) as 1 | -1) : -1 }))}>
                  {c.label}
                  {sort.key === c.key ? (sort.dir === 1 ? ' ▲' : ' ▼') : ''}
                </button>
              </th>
            ))}
            <th className="font-normal">Evidence</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((r) => {
            const met = minSamples !== null && r.verified_runs >= minSamples;
            return (
              <tr key={`${r.provider}/${r.model}/${r.task_type}`} className="border-t border-line/60" data-testid="model-row">
                <td className="py-1 pr-3">
                  <ProviderDot provider={r.provider} />
                  {r.provider}
                </td>
                <td className="pr-3 font-mono text-xs">{r.model}</td>
                <td className="pr-3">{r.task_type}</td>
                <td className="tick-num pr-3 text-right">{r.runs}</td>
                <td className="tick-num pr-3 text-right">{r.verified_runs}</td>
                <td className="tick-num pr-3 text-right" data-testid="success-rate">
                  {isMissing(r.success_rate) || r.verified_runs === 0 ? (
                    <span className="text-faint">{NO_DATA}</span>
                  ) : (
                    <>
                      {pct(r.success_rate.value as number)} <span className={r.verified_runs < LOW_EVIDENCE_N ? 'text-amber' : 'text-muted'}>(n = {r.verified_runs})</span>
                    </>
                  )}
                </td>
                <td className="tick-num pr-3 text-right">{isMissing(r.avg_duration_seconds) ? <span className="text-faint">{NO_DATA}</span> : formatSeconds(r.avg_duration_seconds.value)}</td>
                <td className="tick-num pr-3 text-right">{isMissing(r.avg_tokens) ? <span className="text-faint">{NO_DATA}</span> : Math.round(r.avg_tokens.value as number)}</td>
                <td className="pr-3 text-xs text-faint">{r.last_used ? formatDateTime(r.last_used) : NO_DATA}</td>
                <td>
                  {minSamples === null ? (
                    <span className="text-faint">unknown</span>
                  ) : met ? (
                    <span className="chip text-ok" data-testid="threshold" data-met="true" title={`At least ${minSamples} verified runs: adaptive routing may use this`}>
                      <Check className="size-3" aria-hidden /> threshold met
                    </span>
                  ) : (
                    <span className="chip text-faint" data-testid="threshold" data-met="false" title={`Needs ${minSamples} verified runs before adaptive routing uses it`}>
                      <Minus className="size-3" aria-hidden /> below {minSamples}
                    </span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

const metricText = (m: Metric<unknown>, f: (n: number) => string = (n) => String(n)) => (isMissing(m) ? NO_DATA : f(m.value as number));

export function AgentTable({ rows }: { rows: AgentStatsRow[] }) {
  if (rows.length === 0) return <p className="py-6 text-center text-sm text-faint">{NO_DATA}: no agents yet.</p>;
  return (
    <div className="overflow-auto">
      <table className="w-full text-[13px]" aria-label="Agent history">
        <thead>
          <tr className="label text-left">
            {['Agent', 'Role', 'Provider / model', 'Activity', 'Runs', 'Succeeded', 'Failed', 'Working time', 'Last run'].map((h) => (
              <th key={h} className="py-1 pr-3 font-normal">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((a) => (
            <tr key={a.agent_id} className="border-t border-line/60" data-testid="agent-row">
              <td className="py-1 pr-3">{a.name}</td>
              <td className="pr-3">{a.role}</td>
              <td className="pr-3 font-mono text-xs">
                {a.provider ? (
                  <>
                    <ProviderDot provider={a.provider} />
                    {a.provider} / {a.model ?? 'default'}
                  </>
                ) : (
                  <span className="text-faint">unassigned</span>
                )}
              </td>
              <td className="pr-3">{a.activity}</td>
              <td className="tick-num pr-3" data-testid="agent-runs">{metricText(a.runs)}</td>
              <td className="tick-num pr-3">{metricText(a.succeeded)}</td>
              <td className="tick-num pr-3">{metricText(a.failed)}</td>
              <td className="tick-num pr-3">{isMissing(a.working_seconds) ? NO_DATA : formatSeconds(a.working_seconds.value)}</td>
              <td className="text-xs text-faint">{a.last_run_at ? formatDateTime(a.last_run_at) : NO_DATA}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function AdaptiveCells({ cells }: { cells: AdaptiveCell[] }) {
  if (cells.length === 0) return <p className="py-4 text-sm text-faint">{NO_DATA}: no verified runs per model and task type yet.</p>;
  return (
    <div className="overflow-auto">
      <table className="w-full text-[13px]" aria-label="Adaptive routing cells">
        <thead>
          <tr className="label text-left">
            {['Model', 'Task type', 'Success rate (n)', 'Threshold', 'Adjustment', 'Explanation'].map((h) => (
              <th key={h} className="py-1 pr-3 font-normal">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {cells.map((c) => (
            <tr key={`${c.provider}/${c.model}/${c.task_type}`} className="border-t border-line/60 align-top" data-testid="adaptive-cell" data-applied={c.adjustment.applied}>
              <td className="py-1 pr-3 font-mono text-xs">
                <ProviderDot provider={c.provider} />
                {c.provider} / {c.model}
              </td>
              <td className="pr-3">{c.task_type}</td>
              <td className="tick-num pr-3">
                {isMissing(c.success_rate) ? NO_DATA : `${pct(c.success_rate.value as number)} (n = ${c.success_rate.n})`}
              </td>
              <td className="pr-3">
                <span className={`chip ${c.threshold_met ? 'text-ok' : 'text-faint'}`}>
                  {c.verified_runs}/{c.min_samples} {c.threshold_met ? 'met' : 'not met'}
                </span>
              </td>
              <td className="tick-num pr-3" data-testid="adjustment">
                {c.adjustment.applied ? `${c.adjustment.points > 0 ? '+' : ''}${num(c.adjustment.points)} pts` : <span className="text-faint">none applied</span>}
              </td>
              <td className="text-xs text-muted">{c.adjustment.explanation}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
