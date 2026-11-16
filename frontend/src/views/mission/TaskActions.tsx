import { Ban, Pause, Play, PlayCircle, RotateCcw } from 'lucide-react';
import { useState } from 'react';
import { api } from '../../api/endpoints';
import type { Task, TaskAction } from '../../api/types';
import { ErrorBox } from '../../components/ui';
import { isActionAllowed, TASK_ACTIONS } from '../../lib/transitions';
import { useStore } from '../../store/store';

const META: Record<TaskAction, { label: string; icon: typeof Play; danger?: boolean }> = {
  start: { label: 'Start', icon: Play },
  pause: { label: 'Pause', icon: Pause },
  resume: { label: 'Resume', icon: PlayCircle },
  cancel: { label: 'Cancel', icon: Ban, danger: true },
  retry: { label: 'Retry', icon: RotateCcw },
};

/**
 * Action buttons for a task. Enabled strictly per the mirrored state machine
 * (lib/transitions.ts). `compact` renders only the allowed actions (board cards).
 */
export function TaskActions({ task, compact = false }: { task: Task; compact?: boolean }) {
  const putTask = useStore((s) => s.putTask);
  const [busy, setBusy] = useState<TaskAction | null>(null);
  const [error, setError] = useState<unknown>(null);

  const run = async (action: TaskAction) => {
    setBusy(action);
    setError(null);
    try {
      const res = await api.taskAction(task.id, action);
      if (res && typeof res === 'object' && 'id' in res && 'state' in res) putTask(res);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };

  const actions = compact ? TASK_ACTIONS.filter((a) => isActionAllowed(task.state, a)) : TASK_ACTIONS;
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Task actions">
        {actions.map((a) => {
          const { label, icon: Icon, danger } = META[a];
          const allowed = isActionAllowed(task.state, a);
          return (
            <button
              key={a}
              type="button"
              className={`btn ${compact ? 'btn-sm' : ''} ${danger ? 'btn-danger' : ''}`}
              disabled={!allowed || busy !== null}
              onClick={(e) => {
                e.stopPropagation();
                void run(a);
              }}
              aria-label={`${label} task ${task.title}`}
              title={allowed ? label : `${label} is not allowed in state ${task.state}`}
            >
              <Icon className="size-3.5" aria-hidden />
              {label}
            </button>
          );
        })}
      </div>
      {error ? <ErrorBox error={error} /> : null}
    </div>
  );
}
