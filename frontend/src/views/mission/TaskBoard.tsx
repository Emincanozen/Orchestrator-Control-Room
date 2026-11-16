import { useMemo } from 'react';
import { TASK_STATES, type Task, type TaskState } from '../../api/types';
import { Duration, StateBadge } from '../../components/ui';
import { STATE_COLOR, STATE_LABEL } from '../../design/tokens';
import { isActiveState } from '../../lib/transitions';
import { useNow } from '../../lib/time';
import { activeSeconds } from '../../store/reducer';
import { useStore } from '../../store/store';
import { TaskActions } from './TaskActions';

export interface TaskFilters {
  projectId: string | null;
  state: TaskState | 'ALL';
  search: string;
}

export function filterTasks(tasks: Task[], f: TaskFilters): Task[] {
  const q = f.search.trim().toLowerCase();
  return tasks.filter(
    (t) =>
      (f.projectId === null || t.project_id === f.projectId) &&
      (f.state === 'ALL' || t.state === f.state) &&
      (!q || t.title.toLowerCase().includes(q) || t.id.toLowerCase().includes(q) || t.description.toLowerCase().includes(q)),
  );
}

function TaskCard({ task, onOpen }: { task: Task; onOpen: (id: string) => void }) {
  const clock = useStore((s) => s.data.clocks[task.id]);
  const project = useStore((s) => s.data.projects[task.project_id]);
  const now = useNow();
  const active = isActiveState(task.state);
  return (
    <li>
      <div
        className="pixel-card group flex w-full cursor-pointer flex-col gap-2 p-2.5 text-left hover:border-line-strong"
        style={{ borderLeft: `3px solid ${STATE_COLOR[task.state]}` }}
      >
        <button className="text-left focus-visible:outline-offset-4" onClick={() => onOpen(task.id)} aria-label={`Open task ${task.title}`}>
          <div className="flex items-start justify-between gap-2">
            <span className="text-[13px] font-medium leading-snug">{task.title}</span>
            <span className="tick-num shrink-0 text-[11px] text-faint" title="Priority">
              P{task.priority}
            </span>
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-x-2 text-[11px] text-muted">
            <span className="truncate">{project?.name ?? task.project_id}</span>
            <span className="font-mono text-faint">{task.task_type}</span>
            {active && <Duration seconds={activeSeconds(clock, task, now)} />}
          </div>
          {task.current_operation && active && <div className="mt-1 truncate font-mono text-[11px] text-phosphor">{task.current_operation}</div>}
          {task.state_reason && !active && <div className="mt-1 truncate text-[11px] text-faint">{task.state_reason}</div>}
        </button>
        <TaskActions task={task} compact />
      </div>
    </li>
  );
}

export function TaskBoard({ tasks, onOpen, hideEmpty }: { tasks: Task[]; onOpen: (id: string) => void; hideEmpty: boolean }) {
  const grouped = useMemo(() => {
    const g = Object.fromEntries(TASK_STATES.map((s) => [s, [] as Task[]])) as Record<TaskState, Task[]>;
    for (const t of tasks) g[t.state]?.push(t);
    for (const s of TASK_STATES) g[s].sort((a, b) => b.priority - a.priority || a.created_at.localeCompare(b.created_at));
    return g;
  }, [tasks]);

  const columns = TASK_STATES.filter((s) => !hideEmpty || grouped[s].length > 0);
  return (
    <div className="flex h-full gap-3 overflow-x-auto px-4 pb-4" role="list" aria-label="Task board">
      {columns.map((s) => (
        <section
          key={s}
          role="listitem"
          aria-label={`${STATE_LABEL[s]} tasks`}
          className="flex w-60 shrink-0 flex-col rounded-card border border-line bg-sunken/60"
          data-testid={`col-${s}`}
        >
          <header className="flex items-center justify-between border-b border-line px-2.5 py-2">
            <StateBadge state={s} />
            <span className="tick-num text-xs text-muted" aria-label={`${grouped[s].length} tasks`}>
              {grouped[s].length}
            </span>
          </header>
          <ul className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto p-2">
            {grouped[s].map((t) => (
              <TaskCard key={t.id} task={t} onOpen={onOpen} />
            ))}
            {grouped[s].length === 0 && <li className="py-3 text-center text-[11px] text-faint">none</li>}
          </ul>
        </section>
      ))}
    </div>
  );
}
