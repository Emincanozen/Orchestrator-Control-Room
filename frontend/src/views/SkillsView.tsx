import { useEffect, useMemo, useState } from 'react';
import { api } from '../api/endpoints';
import { SKILL_KINDS, SKILL_STATES } from '../api/types';
import { Async, Empty, ViewHeader } from '../components/ui';
import { useResource } from '../lib/useResource';
import { useStore } from '../store/store';
import { SkillDrawer } from './skills/Drawer';
import { KIND_LABEL, STATE_META, SkillCard } from './skills/parts';

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

export function SkillsView() {
  const rev = useStore((s) => s.data.rev.skills);
  const projects = useStore((s) => s.data.projects);
  const projectList = Object.values(projects);
  const [q, setQ] = useState('');
  const [category, setCategory] = useState('');
  const [kind, setKind] = useState('');
  const [state, setState] = useState('');
  const [projectId, setProjectId] = useState('');
  const [openId, setOpenId] = useState<string | null>(null);
  const dq = useDebounced(q, 250);

  const res = useResource(() => api.skills({ q: dq, category, kind, state, project_id: projectId }), [dq, category, kind, state, projectId, rev]);
  // Unfiltered list only to offer the categories that exist.
  const all = useResource(() => api.skills({}), [rev]);
  const categories = useMemo(() => [...new Set((all.data ?? []).map((s) => s.category).filter((c): c is string => !!c))].sort(), [all.data]);
  const project = projectId ? projects[projectId] : undefined;

  return (
    <div>
      <ViewHeader title="Skills & MCP" subtitle="Catalogue of skills and MCP servers. Downloading fetches files only; activation is per project and needs your approval. Nothing is ever executed by the orchestrator." />
      <div className="flex flex-col gap-4 px-5 pb-8">
        <div className="flex flex-wrap items-center gap-2" role="search">
          <input className="field max-w-sm" type="search" placeholder="Search the catalogue…" aria-label="Search skills" value={q} onChange={(e) => setQ(e.target.value)} />
          <select className="field w-auto" aria-label="Filter by category" value={category} onChange={(e) => setCategory(e.target.value)}>
            <option value="">All categories</option>
            {categories.map((c) => (
              <option key={c}>{c}</option>
            ))}
          </select>
          <select className="field w-auto" aria-label="Filter by kind" value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="">All kinds</option>
            {SKILL_KINDS.map((k) => (
              <option key={k} value={k}>
                {KIND_LABEL[k] ?? k}
              </option>
            ))}
          </select>
          <select className="field w-auto" aria-label="Filter by state" value={state} onChange={(e) => setState(e.target.value)}>
            <option value="">All states</option>
            {SKILL_STATES.map((s) => (
              <option key={s} value={s}>
                {STATE_META[s].label}
              </option>
            ))}
          </select>
          <select className="field w-auto" aria-label="Show activation for project" value={projectId} onChange={(e) => setProjectId(e.target.value)}>
            <option value="">No project selected</option>
            {projectList.map((p) => (
              <option key={p.id} value={p.id}>
                Activation in {p.name}
              </option>
            ))}
          </select>
        </div>
        <Async res={res} what="Skills catalogue">
          {(rows) =>
            rows.length === 0 ? (
              <Empty title="No skills match" hint="Change or clear the filters." />
            ) : (
              <>
                <p className="text-xs text-faint" role="status">
                  {rows.length} {rows.length === 1 ? 'entry' : 'entries'}
                </p>
                <ul className="grid grid-cols-[repeat(auto-fill,minmax(21rem,1fr))] gap-3" aria-label="Skills catalogue">
                  {rows.map((s) => (
                    <SkillCard key={s.id} s={s} project={project} onOpen={() => setOpenId(s.id)} />
                  ))}
                </ul>
              </>
            )
          }
        </Async>
      </div>
      {openId && <SkillDrawer skillId={openId} defaultProjectId={projectId} onClose={() => setOpenId(null)} onChanged={() => res.reload()} />}
    </div>
  );
}
