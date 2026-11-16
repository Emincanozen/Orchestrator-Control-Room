import { useMemo, useState, type FormEvent } from 'react';
import { api } from '../../api/endpoints';
import type { Task } from '../../api/types';
import { ListEditor } from '../../components/ListEditor';
import { ErrorBox, Field, Modal } from '../../components/ui';
import { ROLES, TASK_TYPES } from '../../lib/constants';
import { useResource } from '../../lib/useResource';
import { useStore } from '../../store/store';

export function TaskForm({
  open,
  onOpenChange,
  defaultProjectId,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  defaultProjectId: string | null;
  onCreated?: (t: Task) => void;
}) {
  const projects = useStore((s) => s.data.projects);
  const tasks = useStore((s) => s.data.tasks);
  const putTask = useStore((s) => s.putTask);
  const projectList = Object.values(projects).sort((a, b) => a.name.localeCompare(b.name));
  const models = useResource(() => api.models(), []);

  const [projectId, setProjectId] = useState(defaultProjectId ?? projectList[0]?.id ?? '');
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [priority, setPriority] = useState(50);
  const [taskType, setTaskType] = useState<string>('coding');
  const [role, setRole] = useState('');
  const [deps, setDeps] = useState<string[]>([]);
  const [depFilter, setDepFilter] = useState('');
  const [criteria, setCriteria] = useState<string[]>([]);
  const [verify, setVerify] = useState<string[]>([]);
  const [provider, setProvider] = useState('');
  const [model, setModel] = useState('');
  const [autostart, setAutostart] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const candidates = useMemo(
    () =>
      Object.values(tasks)
        .filter((t) => t.project_id === projectId)
        .filter((t) => !depFilter || t.title.toLowerCase().includes(depFilter.toLowerCase()))
        .sort((a, b) => b.created_at.localeCompare(a.created_at)),
    [tasks, projectId, depFilter],
  );

  const modelList = models.data ?? [];
  const providers = [...new Set(modelList.map((m) => m.provider))];
  const modelsForProvider = modelList.filter((m) => m.provider === provider);
  const haveModels = modelList.length > 0;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const created = await api.createTask({
        project_id: projectId,
        title: title.trim(),
        description,
        task_type: taskType,
        priority,
        role: role || null,
        depends_on: deps,
        acceptance_criteria: criteria,
        verify_commands: verify,
        preferred_provider: provider || null,
        preferred_model: model || null,
        autostart,
      });
      putTask(created);
      onCreated?.(created);
      onOpenChange(false);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal open={open} onOpenChange={onOpenChange} title="Create task" description="Creates a task in state CREATED (or queues it when starting immediately)." wide>
      <form onSubmit={submit} className="grid grid-cols-2 gap-4 p-4">
        <div className="col-span-2">
          <Field label="Title" htmlFor="t-title">
            <input id="t-title" className="field" value={title} onChange={(e) => setTitle(e.target.value)} required maxLength={500} autoFocus />
          </Field>
        </div>
        <div className="col-span-2">
          <Field label="Description" htmlFor="t-desc">
            <textarea id="t-desc" className="field min-h-24" value={description} onChange={(e) => setDescription(e.target.value)} />
          </Field>
        </div>
        <Field label="Project" htmlFor="t-project">
          <select
            id="t-project"
            className="field"
            value={projectId}
            onChange={(e) => {
              setProjectId(e.target.value);
              setDeps([]);
            }}
            required
          >
            <option value="" disabled>
              Select a project
            </option>
            {projectList.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label={`Priority (${priority})`} htmlFor="t-priority" hint="0 to 100, higher runs first">
          <input id="t-priority" type="range" min={0} max={100} value={priority} onChange={(e) => setPriority(Number(e.target.value))} className="accent-[var(--color-phosphor)]" />
        </Field>
        <Field label="Task type" htmlFor="t-type">
          <select id="t-type" className="field" value={taskType} onChange={(e) => setTaskType(e.target.value)}>
            {TASK_TYPES.map((t) => (
              <option key={t}>{t}</option>
            ))}
          </select>
        </Field>
        <Field label="Role" htmlFor="t-role" hint="Leave empty to let the router derive it from the task type">
          <select id="t-role" className="field" value={role} onChange={(e) => setRole(e.target.value)}>
            <option value="">(auto)</option>
            {ROLES.map((r) => (
              <option key={r}>{r}</option>
            ))}
          </select>
        </Field>

        <div className="col-span-2">
          <Field label={`Dependencies (${deps.length} selected)`} htmlFor="t-dep-filter">
            <input id="t-dep-filter" className="field" placeholder="Filter tasks of this project…" value={depFilter} onChange={(e) => setDepFilter(e.target.value)} />
            <ul className="max-h-32 overflow-auto rounded-pixel border border-line bg-sunken" aria-label="Dependency candidates">
              {candidates.map((t) => (
                <li key={t.id}>
                  <label className="flex cursor-pointer items-center gap-2 px-2 py-1 text-[13px] hover:bg-panel">
                    <input
                      type="checkbox"
                      checked={deps.includes(t.id)}
                      onChange={(e) => setDeps(e.target.checked ? [...deps, t.id] : deps.filter((d) => d !== t.id))}
                    />
                    <span className="truncate">{t.title}</span>
                    <span className="ml-auto font-mono text-[10px] text-faint">{t.state}</span>
                  </label>
                </li>
              ))}
              {candidates.length === 0 && <li className="px-2 py-2 text-xs text-faint">No tasks in this project.</li>}
            </ul>
          </Field>
        </div>

        <ListEditor label="Acceptance criteria" items={criteria} onChange={setCriteria} placeholder="e.g. Settings page persists the theme" />
        <ListEditor label="Verify commands" items={verify} onChange={setVerify} mono placeholder="e.g. npm test" />

        <Field label="Preferred provider (optional)" htmlFor="t-provider" hint={haveModels ? undefined : 'Model registry unavailable: free text'}>
          {haveModels ? (
            <select
              id="t-provider"
              className="field"
              value={provider}
              onChange={(e) => {
                setProvider(e.target.value);
                setModel('');
              }}
            >
              <option value="">(router decides)</option>
              {providers.map((p) => (
                <option key={p}>{p}</option>
              ))}
            </select>
          ) : (
            <input id="t-provider" className="field" value={provider} onChange={(e) => setProvider(e.target.value)} />
          )}
        </Field>
        <Field label="Preferred model (optional)" htmlFor="t-model">
          {haveModels ? (
            <select id="t-model" className="field" value={model} onChange={(e) => setModel(e.target.value)} disabled={!provider}>
              <option value="">(provider default)</option>
              {modelsForProvider.map((m) => (
                <option key={m.model_id} value={m.model_id}>
                  {m.display_name ?? m.model_id}
                </option>
              ))}
            </select>
          ) : (
            <input id="t-model" className="field" value={model} onChange={(e) => setModel(e.target.value)} />
          )}
        </Field>

        <label className="col-span-2 flex items-center gap-2 text-[13px]">
          <input type="checkbox" checked={autostart} onChange={(e) => setAutostart(e.target.checked)} />
          Queue immediately after creation
        </label>

        {error ? (
          <div className="col-span-2">
            <ErrorBox error={error} />
          </div>
        ) : null}
        <div className="col-span-2 flex justify-end gap-2">
          <button type="button" className="btn" onClick={() => onOpenChange(false)}>
            Cancel
          </button>
          <button type="submit" className="btn btn-primary" disabled={busy || !title.trim() || !projectId}>
            Create task
          </button>
        </div>
      </form>
    </Modal>
  );
}
