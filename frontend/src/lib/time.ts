import { useEffect, useState } from 'react';

/** Shared 1 Hz clock: durations tick locally from recorded timestamps, no network. */
const subs = new Set<() => void>();
let timer: ReturnType<typeof setInterval> | null = null;
let now = Date.now();

function start() {
  if (timer) return;
  timer = setInterval(() => {
    now = Date.now();
    subs.forEach((s) => s());
  }, 1000);
}

export function useNow(): number {
  const [, setN] = useState(0);
  useEffect(() => {
    const cb = () => setN((n) => n + 1);
    subs.add(cb);
    now = Date.now();
    start();
    cb(); // re-render with the fresh clock: the module value may be stale from before mount
    return () => {
      subs.delete(cb);
      if (subs.size === 0 && timer) {
        clearInterval(timer);
        timer = null;
      }
    };
  }, []);
  return now;
}

export function parseTs(ts: string | null | undefined): number | null {
  if (!ts) return null;
  // Backend stores UTC ISO strings; treat a missing zone as UTC.
  const s = /[zZ]|[+-]\d\d:?\d\d$/.test(ts) ? ts : `${ts}Z`;
  const t = Date.parse(s);
  return Number.isNaN(t) ? null : t;
}

export function formatDuration(totalSeconds: number | null | undefined): string {
  if (totalSeconds === null || totalSeconds === undefined || !Number.isFinite(totalSeconds)) return '--';
  const s = Math.max(0, Math.floor(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n: number) => String(n).padStart(2, '0');
  return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${pad(m)}:${pad(sec)}`;
}

export function formatDateTime(ts: string | null | undefined): string {
  const t = parseTs(ts);
  if (t === null) return '--';
  return new Date(t).toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'medium' });
}

export function formatTime(ts: string | null | undefined): string {
  const t = parseTs(ts);
  if (t === null) return '--';
  return new Date(t).toLocaleTimeString(undefined, { hour12: false });
}

export function relativeAgo(ts: string | null | undefined, nowMs: number): string {
  const t = parseTs(ts);
  if (t === null) return '--';
  const d = Math.max(0, Math.floor((nowMs - t) / 1000));
  if (d < 5) return 'just now';
  if (d < 60) return `${d}s ago`;
  if (d < 3600) return `${Math.floor(d / 60)}m ago`;
  if (d < 86400) return `${Math.floor(d / 3600)}h ago`;
  return `${Math.floor(d / 86400)}d ago`;
}
