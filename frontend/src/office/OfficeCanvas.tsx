import { useEffect, useRef, useState } from 'react';
import { Minus, Plus, Scan } from 'lucide-react';
import type { Agent } from '../api/types';
import { ActivityBadge } from '../components/ui';
import type { OfficeEngine } from './engine/api';
import { createPixiOffice } from './engine/pixiOffice';

export interface OfficeCanvasProps {
  agents: Agent[];
  onSelectAgent: (agentId: string) => void;
  selectedAgentId?: string | null;
}

function prefersReducedMotion(): boolean {
  try {
    return typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch {
    return false;
  }
}

/**
 * The animated pixel office. A thin shell around the self-contained engine in
 * `./engine`: it owns the engine lifecycle (safe under StrictMode double-mount),
 * feeds it the agents it is given and never fetches or writes anything.
 * An offscreen list of focusable buttons mirrors the canvas for keyboard use.
 */
export function OfficeCanvas({ agents, onSelectAgent, selectedAgentId }: OfficeCanvasProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const engineRef = useRef<OfficeEngine | null>(null);
  const latest = useRef({ agents, onSelectAgent, selectedAgentId });
  latest.current = { agents, onSelectAgent, selectedAgentId };
  const [error, setError] = useState<string | null>(null);
  const [zoom, setZoom] = useState(1);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const engine = createPixiOffice({
      reducedMotion: prefersReducedMotion(),
      ariaLabel: 'Pixel office: agents at their desks. Use the agent list for keyboard access.',
      onSelect: (id) => latest.current.onSelectAgent(id),
      onError: (m) => setError(m),
      onZoom: setZoom,
    });
    engineRef.current = engine;
    engine.setAgents(latest.current.agents);
    engine.setSelected(latest.current.selectedAgentId ?? null);
    void engine.mount(host);
    let mq: MediaQueryList | null = null;
    const onMq = () => engine.setReducedMotion(prefersReducedMotion());
    try {
      mq = typeof window.matchMedia === 'function' ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
      mq?.addEventListener?.('change', onMq);
    } catch {
      mq = null;
    }
    return () => {
      mq?.removeEventListener?.('change', onMq);
      engine.destroy();
      if (engineRef.current === engine) engineRef.current = null;
    };
  }, []);

  useEffect(() => {
    engineRef.current?.setAgents(agents);
  }, [agents]);

  useEffect(() => {
    engineRef.current?.setSelected(selectedAgentId ?? null);
  }, [selectedAgentId]);

  if (agents.length === 0) {
    return <div className="p-6 text-sm text-muted">No agents recorded by the backend yet.</div>;
  }

  const btn = 'grid size-7 place-items-center border border-line-strong bg-panel/90 text-ink hover:border-phosphor';
  return (
    <div className="relative h-full min-h-[360px] w-full" data-testid="office-root">
      <div ref={hostRef} className="absolute inset-0 overflow-hidden bg-void" />
      {error && (
        <div role="alert" className="absolute left-3 top-3 max-w-sm border border-danger/50 bg-panel p-2 text-xs text-danger">
          Pixel Office renderer unavailable ({error}). The agent list below remains usable.
        </div>
      )}
      <div className="absolute right-3 top-3 flex items-center gap-1" role="group" aria-label="Office zoom">
        <button type="button" className={btn} aria-label="Zoom out" onClick={() => engineRef.current?.zoomOut()}>
          <Minus className="size-3.5" aria-hidden />
        </button>
        <span className="tick-num min-w-8 text-center text-[11px] text-muted" aria-live="polite">
          x{zoom}
        </span>
        <button type="button" className={btn} aria-label="Zoom in" onClick={() => engineRef.current?.zoomIn()}>
          <Plus className="size-3.5" aria-hidden />
        </button>
        <button type="button" className={btn} aria-label="Fit office to window" onClick={() => engineRef.current?.fit()}>
          <Scan className="size-3.5" aria-hidden />
        </button>
      </div>
      <ul className="sr-only focus-within:not-sr-only focus-within:absolute focus-within:bottom-2 focus-within:left-2 focus-within:right-2 focus-within:z-10 focus-within:max-h-28 focus-within:overflow-y-auto focus-within:flex focus-within:max-w-full focus-within:flex-wrap focus-within:gap-1 focus-within:bg-panel focus-within:p-2" aria-label="Office agents">
        {agents.map((a) => (
          <li key={a.id} data-testid="office-agent" data-agent-id={a.id} data-activity={a.activity} data-task-id={a.current_task_id ?? ''}>
            <button
              type="button"
              className="flex items-center gap-2 border border-line-strong bg-raised px-2 py-1 text-xs"
              aria-pressed={selectedAgentId === a.id}
              onClick={() => onSelectAgent(a.id)}
            >
              {a.name}, {a.role}, {a.room}
              {a.provider ? `, ${a.provider}${a.model ? `/${a.model}` : ''}` : ''}
              <ActivityBadge activity={a.activity} />
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
