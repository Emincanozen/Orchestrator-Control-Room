import type { Metric, TimeseriesPoint } from '../api/types';

/** Evidence below this many samples is flagged as low-evidence in the UI. */
export const LOW_EVIDENCE_N = 5;

export const isMissing = (m: Metric<unknown> | null | undefined): boolean => !m || m.exactness === 'missing' || m.value === null || m.value === undefined;

/** "No data yet" for a missing metric, never a zero. */
export const NO_DATA = 'No data yet';

export const pct = (v: number): string => `${Number((v * 100).toFixed(1))}%`;
export const num = (v: number, digits = 2): string => (Number.isInteger(v) ? String(v) : v.toFixed(digits));
export const usd = (v: number): string => `$${v.toFixed(v < 1 ? 4 : 2)}`;

export function formatSeconds(s: number | null | undefined): string {
  if (s === null || s === undefined || !Number.isFinite(s)) return NO_DATA;
  if (s < 1) return `${Math.round(s * 1000)} ms`;
  if (s < 60) return `${s.toFixed(1)} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
  return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`;
}

export interface GapPoint {
  bucket: string;
  /** null = no data that day (a gap, not a zero) */
  value: number | null;
  n: number;
}

const DAY = 86_400_000;
const dayMs = (b: string) => Date.parse(`${b.slice(0, 10)}T00:00:00Z`);

/**
 * Expands sparse daily points to a continuous day axis. Days the backend omitted get `value: null`, so charts show a
 * gap (and never a zero or an interpolated line). Points are sorted by bucket.
 */
export function fillGaps(points: TimeseriesPoint[]): GapPoint[] {
  if (points.length === 0) return [];
  const sorted = [...points].sort((a, b) => a.bucket.localeCompare(b.bucket));
  const have = new Map(sorted.map((p) => [p.bucket.slice(0, 10), p]));
  const start = dayMs(sorted[0].bucket);
  const end = dayMs(sorted[sorted.length - 1].bucket);
  const out: GapPoint[] = [];
  for (let t = start; t <= end; t += DAY) {
    const key = new Date(t).toISOString().slice(0, 10);
    const p = have.get(key);
    out.push(p ? { bucket: key, value: p.value, n: p.n } : { bucket: key, value: null, n: 0 });
  }
  return out;
}

export interface TokenRow {
  key: string;
  provider: string;
  model: string;
  input: number;
  output: number;
  cached: number | null;
  runs: number;
}

export function tokenRows(by: Record<string, { input: number; output: number; cached: number | null; runs: number }>): TokenRow[] {
  return Object.entries(by)
    .map(([key, v]) => {
      const i = key.indexOf('/');
      return { key, provider: i < 0 ? key : key.slice(0, i), model: i < 0 ? '' : key.slice(i + 1), ...v };
    })
    .sort((a, b) => b.input + b.output - (a.input + a.output));
}
