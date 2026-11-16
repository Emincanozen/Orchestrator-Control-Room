import { Pause, Play } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../api/endpoints';
import type { ApiEvent } from '../api/types';
import { Empty, ErrorBox, ViewHeader } from '../components/ui';
import { formatTime } from '../lib/time';
import { subscribeEvents, useStore } from '../store/store';
import { EMPTY_FILTER, filterEvents, windowRange, type LogFilter } from './logs/filter';

export const ROW_H = 22;
const MAX_EVENTS = 20_000;
const BACKLOG = 3000;

function sortedUnique(map: Map<number, ApiEvent>): ApiEvent[] {
  return [...map.values()].sort((a, b) => a.seq - b.seq);
}

/** Global event stream: REST backlog (GET /events) + live WebSocket events, windowed so thousands of rows stay cheap. */
export function LogsView() {
  const projects = useStore((s) => s.data.projects);
  const lastSeq = useStore((s) => s.data.lastSeq);
  const notices = useStore((s) => s.data.notices);
  const [events, setEvents] = useState<ApiEvent[]>([]);
  const [paused, setPaused] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [filter, setFilter] = useState<LogFilter>(EMPTY_FILTER);
  const [selected, setSelected] = useState<number | null>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewport, setViewport] = useState(480);
  const store = useRef(new Map<number, ApiEvent>());
  const pending = useRef<ApiEvent[]>([]);
  const pausedRef = useRef(false);
  const box = useRef<HTMLDivElement>(null);
  pausedRef.current = paused;

  const commit = () => {
    const m = store.current;
    if (m.size > MAX_EVENTS) {
      const keep = sortedUnique(m).slice(-MAX_EVENTS);
      m.clear();
      keep.forEach((e) => m.set(e.seq, e));
    }
    setEvents(sortedUnique(m));
  };

  // live events: buffered while paused, merged on resume
  useEffect(() => {
    return subscribeEvents((evs) => {
      if (pausedRef.current) {
        pending.current.push(...evs);
        return;
      }
      evs.forEach((e) => store.current.set(e.seq, e));
      commit();
    });
  }, []);

  // backlog: the most recent BACKLOG events before the stream's current position
  const started = useRef(false);
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const after = Math.max(0, useStore.getState().data.lastSeq - BACKLOG);
    api
      .events({ after_seq: after, limit: BACKLOG })
      .then((evs) => {
        evs.forEach((e) => store.current.set(e.seq, e));
        commit();
      })
      .catch(setError);
  }, []);

  const resume = () => {
    pending.current.forEach((e) => store.current.set(e.seq, e));
    pending.current = [];
    setPaused(false);
    commit();
  };

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const measure = () => setViewport(el.clientHeight || 480);
    measure();
    if (typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const rows = useMemo(() => filterEvents(events, filter), [events, filter]);
  const { start, end } = windowRange(scrollTop, viewport, ROW_H, rows.length);
  const sel = selected !== null ? (store.current.get(selected) ?? null) : null;
  const set = (k: keyof LogFilter, v: string) => setFilter((f) => ({ ...f, [k]: v }));
  const typeHint = useMemo(() => [...new Set(events.map((e) => e.type.split('.')[0]))].sort(), [events]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <ViewHeader
        title="Logs"
        subtitle={`Global event stream. ${events.length} events loaded, ${rows.length} shown, newest first. Last server seq #${lastSeq}.`}
        actions={
          <button className="btn" onClick={() => (paused ? resume() : setPaused(true))} aria-pressed={paused} data-testid="pause-toggle">
            {paused ? <Play className="size-3.5" aria-hidden /> : <Pause className="size-3.5" aria-hidden />}
            {paused ? `Resume (${pending.current.length} new)` : 'Pause'}
          </button>
        }
      />
      <div className="flex flex-wrap items-end gap-3 px-5 pb-3" role="search" aria-label="Log filters">
        <label className="flex flex-col gap-1">
          <span className="label">Type prefix</span>
          <input className="field w-44" list="event-prefixes" placeholder="e.g. task. or run.output" aria-label="Event type prefix" value={filter.type} onChange={(e) => set('type', e.target.value)} />
          <datalist id="event-prefixes">
            {typeHint.map((t) => (
              <option key={t} value={`${t}.`} />
            ))}
          </datalist>
        </label>
        <label className="flex flex-col gap-1">
          <span className="label">Project</span>
          <select className="field w-auto" aria-label="Project" value={filter.project} onChange={(e) => set('project', e.target.value)}>
            <option value="">All projects</option>
            {Object.values(projects).map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="label">Task id</span>
          <input className="field w-36" aria-label="Task id" value={filter.task} onChange={(e) => set('task', e.target.value)} />
        </label>
        <label className="flex flex-col gap-1">
          <span className="label">Agent id</span>
          <input className="field w-36" aria-label="Agent id" value={filter.agent} onChange={(e) => set('agent', e.target.value)} />
        </label>
        <label className="flex flex-col gap-1">
          <span className="label">Text</span>
          <input className="field w-56" type="search" placeholder="search type and payload" aria-label="Search events" value={filter.text} onChange={(e) => set('text', e.target.value)} />
        </label>
        {(filter.type || filter.project || filter.task || filter.agent || filter.text) && (
          <button className="btn btn-sm" onClick={() => setFilter(EMPTY_FILTER)}>
            Clear
          </button>
        )}
      </div>
      {error ? (
        <div className="px-5 pb-2">
          <ErrorBox error={error} />
        </div>
      ) : null}
      {notices.length > 0 && <div className="px-5 pb-2 text-xs text-amber" aria-label="Latest system notice">Latest notice: {notices[notices.length - 1].message}</div>}
      <div className="grid min-h-0 flex-1 gap-3 px-5 pb-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,26rem)]">
        {rows.length === 0 ? (
          <Empty title={events.length === 0 ? 'No events received yet' : 'No events match these filters'} />
        ) : (
          <div ref={box} className="min-h-64 overflow-auto border border-line bg-sunken font-mono text-[11px]" style={{ maxHeight: '70vh' }} onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)} role="list" aria-label="Event log" data-testid="event-log" data-rows={rows.length}>
            <div style={{ height: rows.length * ROW_H, position: 'relative' }}>
              {rows.slice(start, end).map((e, i) => (
                <div
                  key={e.seq}
                  role="listitem"
                  tabIndex={0}
                  data-testid="log-row"
                  data-seq={e.seq}
                  aria-selected={selected === e.seq}
                  onClick={() => setSelected(e.seq)}
                  onKeyDown={(k) => (k.key === 'Enter' || k.key === ' ') && setSelected(e.seq)}
                  className={`absolute left-0 right-0 flex cursor-pointer items-center gap-3 border-b border-line/40 px-2 hover:bg-raised ${selected === e.seq ? 'bg-raised' : ''}`}
                  style={{ top: (start + i) * ROW_H, height: ROW_H }}
                >
                  <span className="w-14 shrink-0 text-right text-faint">#{e.seq}</span>
                  <span className="w-16 shrink-0 text-faint">{formatTime(e.ts)}</span>
                  <span className="w-44 shrink-0 truncate text-signal">{e.type}</span>
                  <span className="truncate text-muted">{e.task_id ?? e.agent_id ?? e.project_id ?? ''}</span>
                </div>
              ))}
            </div>
          </div>
        )}
        <aside className="pixel-card min-h-32 overflow-auto p-3" aria-label="Event detail" data-testid="event-detail">
          {sel ? (
            <div className="flex flex-col gap-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="chip text-signal">{sel.type}</span>
                <span className="text-xs text-faint">#{sel.seq}</span>
              </div>
              <dl className="grid grid-cols-[auto_1fr] gap-x-3 text-xs text-muted">
                <dt>time</dt>
                <dd>{sel.ts}</dd>
                <dt>project</dt>
                <dd>{sel.project_id ? (projects[sel.project_id]?.name ?? sel.project_id) : '-'}</dd>
                <dt>task</dt>
                <dd>
                  {sel.task_id ? (
                    <a className="text-signal underline" href={`#/mission?task=${encodeURIComponent(sel.task_id)}`} data-testid="jump-task">
                      {sel.task_id} (open)
                    </a>
                  ) : (
                    '-'
                  )}
                </dd>
                <dt>agent</dt>
                <dd>{sel.agent_id ?? '-'}</dd>
                <dt>run</dt>
                <dd>{sel.run_id ?? '-'}</dd>
              </dl>
              <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words bg-void p-2 font-mono text-[11px]" data-testid="payload">
                {JSON.stringify(sel.payload, null, 2)}
              </pre>
            </div>
          ) : (
            <span className="text-xs text-faint">Select an event to see its payload as formatted JSON (shown as text only).</span>
          )}
        </aside>
      </div>
    </div>
  );
}
