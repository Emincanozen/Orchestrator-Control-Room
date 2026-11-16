import type { ApiEvent } from '../../api/types';

export interface LogFilter {
  /** event type prefix, e.g. "task." or "run.output" */
  type: string;
  project: string;
  task: string;
  agent: string;
  text: string;
}

export const EMPTY_FILTER: LogFilter = { type: '', project: '', task: '', agent: '', text: '' };

const haystacks = new WeakMap<ApiEvent, string>();
/** Lower-cased searchable text of an event, computed once per event object. */
function haystack(e: ApiEvent): string {
  let h = haystacks.get(e);
  if (h === undefined) {
    h = `${e.type} ${e.task_id ?? ''} ${e.agent_id ?? ''} ${e.run_id ?? ''} ${safeJson(e.payload)}`.toLowerCase();
    haystacks.set(e, h);
  }
  return h;
}

function safeJson(v: unknown): string {
  try {
    return JSON.stringify(v) ?? '';
  } catch {
    return '';
  }
}

export function matches(e: ApiEvent, f: LogFilter): boolean {
  if (f.type && !e.type.startsWith(f.type.trim())) return false;
  if (f.project && e.project_id !== f.project) return false;
  if (f.task && !(e.task_id ?? '').toLowerCase().includes(f.task.trim().toLowerCase())) return false;
  if (f.agent && !(e.agent_id ?? '').toLowerCase().includes(f.agent.trim().toLowerCase())) return false;
  const t = f.text.trim().toLowerCase();
  if (t && !haystack(e).includes(t)) return false;
  return true;
}

/** Newest first. */
export function filterEvents(events: ApiEvent[], f: LogFilter): ApiEvent[] {
  const out: ApiEvent[] = [];
  for (let i = events.length - 1; i >= 0; i -= 1) if (matches(events[i], f)) out.push(events[i]);
  return out;
}

/** Window of rows to render for a fixed row height. */
export function windowRange(scrollTop: number, viewport: number, rowH: number, total: number, overscan = 8): { start: number; end: number } {
  const start = Math.max(0, Math.floor(scrollTop / rowH) - overscan);
  const end = Math.min(total, Math.ceil((scrollTop + viewport) / rowH) + overscan);
  return { start, end };
}
