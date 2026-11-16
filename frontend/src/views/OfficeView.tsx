import { lazy, Suspense, useMemo, useState } from 'react';
import { AgentInspector } from '../components/AgentInspector';
import { Spinner, Unavailable, ViewHeader } from '../components/ui';
import type { Agent } from '../api/types';
import { useStore } from '../store/store';

const OfficeCanvas = lazy(() => import('../office/OfficeCanvas').then((m) => ({ default: m.OfficeCanvas })));
// Dev-only: the dynamic import is removed from production builds.
const OfficePreview = import.meta.env.DEV ? lazy(() => import('../office/OfficePreview')) : null;

export function OfficeView() {
  const agentsMap = useStore((s) => s.data.agents);
  const unavailable = useStore((s) => s.unavailable.agents);
  const [selected, setSelected] = useState<string | null>(null);
  const [preview, setPreview] = useState<Agent[] | null>(null);
  const real = useMemo(() => Object.values(agentsMap), [agentsMap]);
  const agents = preview ?? real;
  const inspectable = selected !== null && real.some((a) => a.id === selected);
  return (
    <div className="flex h-full min-h-0 flex-col">
      <ViewHeader title="Office" subtitle="Agents and their recorded activity" />
      {OfficePreview && (
        <Suspense fallback={null}>
          <OfficePreview agents={real} onAgents={setPreview} />
        </Suspense>
      )}
      {unavailable && !preview ? (
        <Unavailable what="Agents" />
      ) : (
        <div className="flex min-h-0 flex-1">
          <div className="min-w-0 flex-1">
            <Suspense fallback={<div className="p-6"><Spinner /></div>}>
              <OfficeCanvas agents={agents} onSelectAgent={setSelected} selectedAgentId={selected} />
            </Suspense>
          </div>
          {inspectable && selected && (
            <aside className="w-80 shrink-0 overflow-auto border-l border-line bg-void/30" aria-label="Agent inspector">
              <AgentInspector agentId={selected} />
            </aside>
          )}
        </div>
      )}
    </div>
  );
}
