import type { ReactNode } from 'react';
import { Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { TIMESERIES_METRICS, type Metric, type Percentiles, type Timeseries, type TokensValue } from '../../api/types';
import { ExactnessChip } from '../../components/ui';
import { providerColor } from '../../design/tokens';
import { NO_DATA, fillGaps, formatSeconds, isMissing, tokenRows, usd } from '../../lib/analytics';

const AXIS = { stroke: 'var(--color-faint)', fontSize: 11, tickLine: false } as const;
const GRID = 'var(--color-line)';
const TIP = {
  contentStyle: { background: 'var(--color-raised)', border: '1px solid var(--color-line-strong)', borderRadius: 2, fontSize: 12 },
  labelStyle: { color: 'var(--color-ink)' },
  itemStyle: { color: 'var(--color-ink)' },
  cursor: { fill: 'rgba(255,255,255,0.04)' },
} as const;
const NEUTRAL = 'var(--color-cat-neutral)';

export function ChartCard({ title, caption, metric, children, table, id }: { title: string; caption?: string; metric?: Metric<unknown>; children: ReactNode; table?: ReactNode; id: string }) {
  return (
    <section className="pixel-card flex flex-col gap-2 p-4" aria-label={title} data-testid={`chart-${id}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="label">{title}</h3>
        {metric && (
          <span className="flex items-center gap-2 text-[11px] text-muted">
            <ExactnessChip exactness={metric.exactness} /> n = {metric.n}
          </span>
        )}
      </div>
      {caption && <p className="text-[11px] text-faint">{caption}</p>}
      {children}
      {table && (
        <details className="text-xs text-muted">
          <summary className="cursor-pointer select-none">Show as table</summary>
          <div className="mt-2">{table}</div>
        </details>
      )}
    </section>
  );
}

export function NoData({ why }: { why?: string }) {
  return (
    <div className="flex h-40 flex-col items-center justify-center gap-1 text-center" data-testid="no-data">
      <div className="text-base text-faint">{NO_DATA}</div>
      {why && <div className="max-w-sm text-[11px] text-faint">{why}</div>}
    </div>
  );
}

const SERIES_LABEL: Record<string, { label: string; unit: string; line: boolean }> = {
  tasks_completed: { label: 'Tasks completed per day', unit: 'tasks', line: false },
  runs_started: { label: 'Runs started per day', unit: 'runs', line: false },
  runs_failed: { label: 'Runs failed per day', unit: 'runs', line: false },
  test_pass_rate: { label: 'Test pass rate per day', unit: '%', line: true },
  tokens_input: { label: 'Input tokens per day', unit: 'tokens', line: false },
  tokens_output: { label: 'Output tokens per day', unit: 'tokens', line: false },
  cost_usd_estimated: { label: 'Estimated cost per day (list-price equivalent, not billed)', unit: 'USD', line: false },
};

export function TimeseriesChart({ ts, metric, onMetric }: { ts: Timeseries | null; metric: string; onMetric: (m: string) => void }) {
  const info = SERIES_LABEL[metric] ?? { label: metric, unit: '', line: false };
  const isRate = metric === 'test_pass_rate';
  const data = ts ? fillGaps(ts.points).map((p) => ({ ...p, y: p.value === null ? null : isRate ? p.value * 100 : p.value })) : [];
  const gaps = data.filter((d) => d.value === null).length;
  return (
    <ChartCard
      id="timeseries"
      title={info.label}
      caption="Days without data are left empty; they are not counted as zero and no line crosses them."
      metric={ts ? { value: null, exactness: ts.exactness, source: ts.source, n: ts.n } : undefined}
      table={
        data.length > 0 ? (
          <table className="w-full" aria-label="Time series values">
            <thead>
              <tr className="label text-left">
                <th className="font-normal">Day</th>
                <th className="font-normal">{info.unit}</th>
                <th className="font-normal">n</th>
              </tr>
            </thead>
            <tbody>
              {data.map((d) => (
                <tr key={d.bucket}>
                  <td>{d.bucket}</td>
                  <td>{d.value === null ? NO_DATA : isRate ? `${(d.value * 100).toFixed(1)}%` : d.value}</td>
                  <td>{d.n}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : undefined
      }
    >
      <div className="flex items-center gap-2">
        <label htmlFor="ts-metric" className="label">
          Metric
        </label>
        <select id="ts-metric" className="field w-auto" value={metric} onChange={(e) => onMetric(e.target.value)}>
          {TIMESERIES_METRICS.map((m) => (
            <option key={m} value={m}>
              {SERIES_LABEL[m]?.label ?? m}
            </option>
          ))}
        </select>
      </div>
      {!ts || data.length === 0 ? (
        <NoData why="No days with data for this metric and filter." />
      ) : (
        <div role="img" aria-label={`${info.label}: ${ts.points.length} days with data${gaps ? `, ${gaps} days without data shown as gaps` : ''}.`} className="h-64 w-full" data-testid="timeseries-chart" data-gaps={gaps}>
          <ResponsiveContainer width="100%" height="100%">
            {info.line ? (
              <LineChart data={data} margin={{ top: 8, right: 16, bottom: 20, left: 8 }}>
                <CartesianGrid stroke={GRID} vertical={false} />
                <XAxis dataKey="bucket" {...AXIS} label={{ value: 'Day (UTC)', position: 'insideBottom', offset: -10, fill: 'var(--color-muted)', fontSize: 11 }} />
                <YAxis {...AXIS} domain={[0, 100]} unit="%" label={{ value: 'Pass rate (%)', angle: -90, position: 'insideLeft', fill: 'var(--color-muted)', fontSize: 11 }} />
                <Tooltip {...TIP} formatter={(v) => [`${Number(v).toFixed(1)}%`, 'pass rate']} />
                <Line type="linear" dataKey="y" name="Pass rate" stroke={NEUTRAL} strokeWidth={2} dot={{ r: 4, fill: NEUTRAL, stroke: 'var(--color-panel)', strokeWidth: 2 }} connectNulls={false} isAnimationActive={false} />
              </LineChart>
            ) : (
              <BarChart data={data} margin={{ top: 8, right: 16, bottom: 20, left: 8 }}>
                <CartesianGrid stroke={GRID} vertical={false} />
                <XAxis dataKey="bucket" {...AXIS} label={{ value: 'Day (UTC)', position: 'insideBottom', offset: -10, fill: 'var(--color-muted)', fontSize: 11 }} />
                <YAxis {...AXIS} allowDecimals={metric === 'cost_usd_estimated'} label={{ value: info.unit, angle: -90, position: 'insideLeft', fill: 'var(--color-muted)', fontSize: 11 }} />
                <Tooltip {...TIP} formatter={(v) => [metric === 'cost_usd_estimated' ? usd(Number(v)) : String(v), info.unit]} />
                <Bar dataKey="y" name={info.label} fill={NEUTRAL} radius={[2, 2, 0, 0]} maxBarSize={36} isAnimationActive={false} />
              </BarChart>
            )}
          </ResponsiveContainer>
        </div>
      )}
    </ChartCard>
  );
}

export function FailureChart({ metric }: { metric: Metric<Record<string, number> | null> | undefined }) {
  const missing = isMissing(metric);
  const rows = missing ? [] : Object.entries(metric!.value!).map(([category, count]) => ({ category, count })).sort((a, b) => b.count - a.count);
  return (
    <ChartCard
      id="failures"
      title="Failed runs by error category"
      caption="Counts of failed runs; categories come from the run error classification."
      metric={metric}
      table={
        rows.length > 0 ? (
          <table className="w-full" aria-label="Failures by category">
            <tbody>
              {rows.map((r) => (
                <tr key={r.category}>
                  <td>{r.category}</td>
                  <td className="tick-num text-right">{r.count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : undefined
      }
    >
      {missing ? (
        <NoData why="No runs in this scope yet." />
      ) : rows.length === 0 ? (
        <div className="flex h-24 items-center justify-center text-sm text-ok" data-testid="no-failures">
          0 failed runs among {metric!.n === 0 ? 'the runs in scope' : `${metric!.n} runs`}.
        </div>
      ) : (
        <div role="img" aria-label={`Failed runs by category: ${rows.map((r) => `${r.category} ${r.count}`).join(', ')}`} style={{ height: 36 * rows.length + 48 }} className="w-full">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={rows} layout="vertical" margin={{ top: 4, right: 32, bottom: 20, left: 8 }}>
              <CartesianGrid stroke={GRID} horizontal={false} />
              <XAxis type="number" allowDecimals={false} {...AXIS} label={{ value: 'Failed runs', position: 'insideBottom', offset: -10, fill: 'var(--color-muted)', fontSize: 11 }} />
              <YAxis type="category" dataKey="category" width={130} {...AXIS} />
              <Tooltip {...TIP} />
              <Bar dataKey="count" name="Failed runs" fill={NEUTRAL} radius={[0, 2, 2, 0]} maxBarSize={22} label={{ position: 'right', fill: 'var(--color-ink)', fontSize: 11 }} isAnimationActive={false} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}
    </ChartCard>
  );
}

export function DurationChart({ metric, title, id }: { metric: Metric<Percentiles | null> | undefined; title: string; id: string }) {
  const missing = isMissing(metric);
  const v = metric?.value;
  const rows = missing || !v ? [] : [{ name: 'p50 (median)', secs: v.p50 }, { name: 'p90', secs: v.p90 }, { name: 'mean', secs: v.mean }].filter((r) => r.secs !== null);
  return (
    <ChartCard
      id={id}
      title={title}
      metric={metric}
      table={
        rows.length > 0 ? (
          <table className="w-full" aria-label={`${title} values`}>
            <tbody>
              {rows.map((r) => (
                <tr key={r.name}>
                  <td>{r.name}</td>
                  <td className="tick-num text-right">{formatSeconds(r.secs)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : undefined
      }
    >
      {missing || rows.length === 0 ? (
        <NoData why="No finished runs in this scope yet." />
      ) : (
        <div role="img" aria-label={`${title}: ${rows.map((r) => `${r.name} ${formatSeconds(r.secs)}`).join(', ')}`} className="h-36 w-full">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={rows} layout="vertical" margin={{ top: 4, right: 64, bottom: 20, left: 8 }}>
              <CartesianGrid stroke={GRID} horizontal={false} />
              <XAxis type="number" {...AXIS} unit=" s" label={{ value: 'Seconds', position: 'insideBottom', offset: -10, fill: 'var(--color-muted)', fontSize: 11 }} />
              <YAxis type="category" dataKey="name" width={96} {...AXIS} />
              <Tooltip {...TIP} formatter={(x) => [formatSeconds(Number(x)), 'duration']} />
              <Bar dataKey="secs" fill={NEUTRAL} radius={[0, 2, 2, 0]} maxBarSize={20} label={{ position: 'right', fill: 'var(--color-ink)', fontSize: 11, formatter: (x: unknown) => formatSeconds(Number(x)) }} isAnimationActive={false} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}
    </ChartCard>
  );
}

export function TokensChart({ tokens, cost }: { tokens: Metric<TokensValue | null> | undefined; cost: Metric | undefined }) {
  const missing = isMissing(tokens);
  const rows = missing ? [] : tokenRows(tokens!.value!.by_model);
  const providers = [...new Set(rows.map((r) => r.provider))];
  // one bar per model: input in the provider colour, output as the lighter stack segment of the same provider
  const data = rows.map((r) => ({ name: r.model ? `${r.provider}/${r.model}` : r.provider, provider: r.provider, input: r.input, output: r.output }));
  return (
    <ChartCard
      id="tokens"
      title="Tokens by model"
      caption="Provider-reported token counts; runs without usage data are not counted."
      metric={tokens}
      table={
        rows.length > 0 ? (
          <table className="w-full" aria-label="Tokens by model">
            <thead>
              <tr className="label text-left">
                <th className="font-normal">Model</th>
                <th className="text-right font-normal">Input</th>
                <th className="text-right font-normal">Output</th>
                <th className="text-right font-normal">Cached</th>
                <th className="text-right font-normal">Runs</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.key}>
                  <td>{r.key}</td>
                  <td className="tick-num text-right">{r.input}</td>
                  <td className="tick-num text-right">{r.output}</td>
                  <td className="tick-num text-right">{r.cached ?? NO_DATA}</td>
                  <td className="tick-num text-right">{r.runs}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : undefined
      }
    >
      {missing ? (
        <NoData why="No run has reported token usage yet (the mock provider and some CLIs do not report it)." />
      ) : (
        <>
          <div role="img" aria-label={`Tokens by model: ${data.map((d) => `${d.name} input ${d.input}, output ${d.output}`).join('; ')}`} style={{ height: 40 * data.length + 56 }} className="w-full">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={data} layout="vertical" margin={{ top: 4, right: 24, bottom: 20, left: 8 }}>
                <CartesianGrid stroke={GRID} horizontal={false} />
                <XAxis type="number" {...AXIS} label={{ value: 'Tokens (input + output)', position: 'insideBottom', offset: -10, fill: 'var(--color-muted)', fontSize: 11 }} />
                <YAxis type="category" dataKey="name" width={150} {...AXIS} />
                <Tooltip {...TIP} />
                <Legend content={() => null} />
                <Bar dataKey="input" name="Input tokens" stackId="t" isAnimationActive={false} fill={NEUTRAL} shape={<ProviderBar solid />} />
                <Bar dataKey="output" name="Output tokens" stackId="t" isAnimationActive={false} fill={NEUTRAL} shape={<ProviderBar />} />
              </BarChart>
            </ResponsiveContainer>
          </div>
          <ul className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted" aria-label="Legend">
            {providers.map((p) => (
              <li key={p} className="flex items-center gap-1.5">
                <span aria-hidden className="inline-block size-2.5" style={{ background: providerColor(p) }} />
                {p}
              </li>
            ))}
            <li>solid = input tokens, lighter = output tokens</li>
          </ul>
        </>
      )}
      <div className="flex flex-wrap items-center gap-2 border-t border-line pt-2 text-xs" data-testid="cost-line">
        <span className="label">Estimated cost</span>
        {isMissing(cost) ? (
          <span className="text-faint">{NO_DATA}</span>
        ) : (
          <>
            <span className="tick-num text-sm">{usd(cost!.value as number)}</span>
            <ExactnessChip exactness={cost!.exactness} />
            <span className="text-muted">n = {cost!.n}</span>
          </>
        )}
        <span className="text-faint">estimated: Claude list-price equivalent, not billed (subscription CLI)</span>
      </div>
    </ChartCard>
  );
}

/** Custom bar segment: provider colour, full opacity for input and 55% for output, 2px surface gap between stacks. */
function ProviderBar(props: { x?: number; y?: number; width?: number; height?: number; payload?: { provider: string }; solid?: boolean }) {
  const { x = 0, y = 0, width = 0, height = 0, payload, solid } = props;
  if (width <= 0) return null;
  return <rect x={x} y={y} width={width} height={height} fill={providerColor(payload?.provider)} fillOpacity={solid ? 1 : 0.5} stroke="var(--color-panel)" strokeWidth={2} />;
}
