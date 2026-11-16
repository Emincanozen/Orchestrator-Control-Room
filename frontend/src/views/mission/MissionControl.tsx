import { Plus, Search } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { TASK_STATES, type TaskState } from '../../api/types';
import { STATE_LABEL } from '../../design/tokens';
import { Empty, ViewHeader } from '../../components/ui';
import { useStore } from '../../store/store';
import { ProjectsPanel } from './ProjectsPanel';
import { TaskBoard, filterTasks } from './TaskBoard';
import { TaskDetail } from './TaskDetail';
import { TaskForm } from './TaskForm';
import { ObjectiveButton, ObjectiveDetail, ObjectiveForm, ObjectivesList } from './Objectives';

export function MissionControl() {
  const tasks = useStore((s) => s.data.tasks);
  const projects = useStore((s) => s.data.projects);
  const loaded = useStore((s) => s.data.loaded);
  const [projectId, setProjectId] = useState<string | null>(null);
  const [state, setState] = useState<TaskState | 'ALL'>('ALL');
  const [search, setSearch] = useState('');
  const [hideEmpty, setHideEmpty] = useState(false);
  const [creating, setCreating] = useState(false);
  const [params, setParams] = useSearchParams();
  const [openId, setOpenIdState] = useState<string | null>(params.get('task'));
  const [objectiveId, setObjectiveId] = useState<string | null>(params.get('objective'));
  const [newObjective, setNewObjective] = useState(false);
  const [objectives, setObjectives] = useState(false);
  useEffect(() => {
    const t = params.get('task');
    const o = params.get('objective');
    if (t) setOpenIdState(t);
    if (o) setObjectiveId(o);
  }, [params]);
  const setOpenId = (id: string | null) => {
    setOpenIdState(id);
    if (params.has('task') || params.has('objective')) setParams({}, { replace: true });
  };

  const filtered = useMemo(
    () => filterTasks(Object.values(tasks), { projectId, state, search }),
    [tasks, projectId, state, search],
  );
  const noProjects = loaded && Object.keys(projects).length === 0;

  return (
    <div className="flex h-full min-h-0">
      <ProjectsPanel selected={projectId} onSelect={setProjectId} />
      <section className="flex min-w-0 flex-1 flex-col" aria-label="Task board">
        <ViewHeader
          title="Mission Control"
          subtitle={`${filtered.length} task${filtered.length === 1 ? '' : 's'} shown`}
          actions={
            <>
            <button className="btn" onClick={() => setObjectives(true)} disabled={noProjects}>
              Objectives
            </button>
            <span className={noProjects ? 'pointer-events-none opacity-40' : ''}>
              <ObjectiveButton onClick={() => setNewObjective(true)} />
            </span>
            <button className="btn btn-primary" onClick={() => setCreating(true)} disabled={noProjects} title={noProjects ? 'Add a project first' : undefined}>
              <Plus className="size-3.5" aria-hidden /> New task
            </button>
            </>
          }
        />
        <div className="flex flex-wrap items-center gap-3 px-5 pb-3">
          <div className="relative">
            <Search className="pointer-events-none absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-faint" aria-hidden />
            <input className="field w-64 pl-7" type="search" placeholder="Search title, description, id" aria-label="Search tasks" value={search} onChange={(e) => setSearch(e.target.value)} />
          </div>
          <label className="flex items-center gap-2 text-xs text-muted">
            <span className="label">State</span>
            <select className="field w-auto" aria-label="Filter by state" value={state} onChange={(e) => setState(e.target.value as TaskState | 'ALL')}>
              <option value="ALL">All states</option>
              {TASK_STATES.map((s) => (
                <option key={s} value={s}>
                  {STATE_LABEL[s]}
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-2 text-xs text-muted">
            <input type="checkbox" checked={hideEmpty} onChange={(e) => setHideEmpty(e.target.checked)} />
            Hide empty columns
          </label>
        </div>
        <div className="min-h-0 flex-1">
          {noProjects ? (
            <Empty title="No projects yet" hint="Register a project (working directory) in the left panel, then create tasks for it." />
          ) : (
            <TaskBoard tasks={filtered} onOpen={setOpenId} hideEmpty={hideEmpty} />
          )}
        </div>
      </section>
      {creating && <TaskForm open onOpenChange={(o) => !o && setCreating(false)} defaultProjectId={projectId} onCreated={(t) => setOpenId(t.id)} />}
      {newObjective && (
        <ObjectiveForm
          defaultProjectId={projectId}
          onClose={() => setNewObjective(false)}
          onCreated={(o) => {
            setNewObjective(false);
            setObjectiveId(o.id);
          }}
        />
      )}
      {objectives && <ObjectivesList projectId={projectId} onClose={() => setObjectives(false)} onOpen={(id) => { setObjectives(false); setObjectiveId(id); }} />}
      {objectiveId && <ObjectiveDetail objectiveId={objectiveId} onClose={() => setObjectiveId(null)} onOpenTask={(id) => { setObjectiveId(null); setOpenId(id); }} />}
      {openId && <TaskDetail taskId={openId} onClose={() => setOpenId(null)} />}
    </div>
  );
}
