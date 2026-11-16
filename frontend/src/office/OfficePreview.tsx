/**
 * DEV-ONLY "Office preview". Lazily imported behind `import.meta.env.DEV`, so it
 * is never part of the production build. It only produces a local override of
 * the agents handed to the canvas; it never writes to the store or the API.
 */
import { useEffect, useMemo, useState } from 'react';
import type { Agent, AgentActivity } from '../api/types';
import { ACTIVITIES } from './engine/behavior';

const ROSTER: [string, string, string, string, string | null, string | null][] = [
  ['agt_leader', 'Leader', 'leader', 'leadership', 'claude', 'claude-opus'],
  ['agt_advisor', 'Advisor', 'advisor', 'leadership', 'codex', 'gpt-5'],
  ['agt_backend_1', 'Backend 1', 'backend', 'backend', 'claude', 'claude-sonnet'],
  ['agt_backend_2', 'Backend 2', 'backend', 'backend', 'codex', null],
  ['agt_frontend_1', 'Frontend 1', 'frontend', 'frontend', 'claude', 'claude-sonnet'],
  ['agt_frontend_2', 'Frontend 2', 'frontend', 'frontend', 'antigravity', 'gemini-pro'],
  ['agt_research', 'Research', 'research', 'research', 'antigravity', 'gemini-pro'],
  ['agt_test', 'Test', 'test', 'testing', 'claude', 'claude-haiku'],
  ['agt_visual', 'Visual', 'visual', 'visual', null, null],
  ['agt_reviewer', 'Reviewer', 'reviewer', 'meeting', 'codex', 'gpt-5'],
];

/** Local fake roster, only used by the preview when the backend has no agents. */
export const DEMO_AGENTS: Agent[] = ROSTER.map(([id, name, role, room, provider, model], i) => ({
  id,
  name,
  role,
  room,
  provider,
  model,
  activity: 'idle',
  current_task_id: null,
  current_run_id: null,
  current_operation: null,
  since: new Date(0).toISOString(),
  seat_order: i % 2,
}));

export interface OfficePreviewProps {
  agents: Agent[];
  /** receives the agents to display (with the preview override applied), or null when the preview is off */
  onAgents: (agents: Agent[] | null) => void;
}

export default function OfficePreview({ agents, onAgents }: OfficePreviewProps) {
  const [on, setOn] = useState(false);
  const [demo, setDemo] = useState(agents.length === 0);
  const [target, setTarget] = useState<string>('');
  const [activity, setActivity] = useState<AgentActivity | ''>('');
  const [cycle, setCycle] = useState(false);

  const base = demo ? DEMO_AGENTS : agents;
  const targetId = base.some((a) => a.id === target) ? target : (base[0]?.id ?? '');

  useEffect(() => {
    if (!cycle) return;
    const order = ACTIVITIES as readonly string[];
    let i = Math.max(0, order.indexOf(activity || 'idle'));
    const t = setInterval(() => {
      i = (i + 1) % order.length;
      setActivity(order[i] as AgentActivity);
    }, 9000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cycle]);

  const out = useMemo(() => {
    if (!on) return null;
    return base.map((a) => (a.id === targetId && activity ? { ...a, activity } : a));
  }, [on, base, targetId, activity]);

  useEffect(() => {
    onAgents(out);
  }, [out, onAgents]);
  useEffect(() => () => onAgents(null), [onAgents]);

  const sel = 'border border-line-strong bg-panel px-1.5 py-0.5 text-xs';
  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-amber/40 bg-amber/5 px-4 py-1.5 text-xs" data-testid="office-preview">
      <span className="chip text-amber">Office preview (dev only, local, not recorded)</span>
      <label className="flex items-center gap-1">
        <input type="checkbox" checked={on} onChange={(e) => setOn(e.target.checked)} /> enable
      </label>
      {on && (
        <>
          <label className="flex items-center gap-1">
            <input type="checkbox" checked={demo} onChange={(e) => setDemo(e.target.checked)} /> demo roster
          </label>
          <select className={sel} aria-label="Preview agent" value={targetId} onChange={(e) => setTarget(e.target.value)}>
            {base.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </select>
          <select className={sel} aria-label="Preview activity" value={activity} onChange={(e) => setActivity(e.target.value as AgentActivity)}>
            <option value="">(recorded)</option>
            {ACTIVITIES.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
          <label className="flex items-center gap-1">
            <input type="checkbox" checked={cycle} onChange={(e) => setCycle(e.target.checked)} /> auto-cycle (9s)
          </label>
        </>
      )}
    </div>
  );
}
