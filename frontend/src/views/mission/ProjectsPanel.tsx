import { FolderGit2, Pencil, Plus, Trash2 } from 'lucide-react';
import { useState, type FormEvent } from 'react';
import { api } from '../../api/endpoints';
import type { Project } from '../../api/types';
import { ListEditor } from '../../components/ListEditor';
import { ErrorBox, Field, Modal } from '../../components/ui';
import { verifyListToStrings } from '../../lib/verify';
import { useStore } from '../../store/store';

export function ProjectForm({ project, open, onOpenChange }: { project: Project | null; open: boolean; onOpenChange: (o: boolean) => void }) {
  const putProject = useStore((s) => s.putProject);
  const initialVerify = verifyListToStrings(project?.verify_commands);
  const [name, setName] = useState(project?.name ?? '');
  const [path, setPath] = useState(project?.path ?? '');
  const [verify, setVerify] = useState<string[]>(initialVerify);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      let saved: Project;
      if (project) {
        const changed = JSON.stringify(verify) !== JSON.stringify(initialVerify);
        saved = await api.updateProject(project.id, { name: name.trim(), ...(changed ? { verify_commands: verify } : {}) });
      } else {
        saved = await api.createProject({ name: name.trim(), path: path.trim(), verify_commands: verify });
      }
      putProject(saved);
      onOpenChange(false);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal open={open} onOpenChange={onOpenChange} title={project ? 'Edit project' : 'Add project'} description="Projects are registered workspaces the agents may work in.">
      <form onSubmit={submit} className="flex flex-col gap-4 p-4">
        <Field label="Name" htmlFor="prj-name">
          <input id="prj-name" className="field" value={name} onChange={(e) => setName(e.target.value)} required maxLength={200} autoFocus />
        </Field>
        <Field label="Working directory path" htmlFor="prj-path" hint={project ? 'The path cannot be changed after registration.' : 'Absolute path to an existing directory. The backend validates it.'}>
          <input
            id="prj-path"
            className="field font-mono"
            value={path}
            onChange={(e) => setPath(e.target.value)}
            required
            readOnly={!!project}
            placeholder="D:\work\my-project"
          />
        </Field>
        <ListEditor label="Verify commands (optional)" items={verify} onChange={setVerify} mono placeholder="e.g. npm test" />
        {error ? <ErrorBox error={error} /> : null}
        <div className="flex justify-end gap-2">
          <button type="button" className="btn" onClick={() => onOpenChange(false)}>
            Cancel
          </button>
          <button type="submit" className="btn btn-primary" disabled={busy || !name.trim() || !path.trim()}>
            {project ? 'Save' : 'Add project'}
          </button>
        </div>
      </form>
    </Modal>
  );
}

function DeleteProject({ project, onClose }: { project: Project; onClose: () => void }) {
  const dropProject = useStore((s) => s.dropProject);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const del = async () => {
    setBusy(true);
    try {
      await api.deleteProject(project.id);
      dropProject(project.id);
      onClose();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal open onOpenChange={(o) => !o && onClose()} title="Delete project" description="This removes the registration, not the files on disk.">
      <div className="flex flex-col gap-4 p-4">
        <p className="text-sm">
          Delete <b>{project.name}</b>? The backend may refuse if tasks still reference it.
        </p>
        {error ? <ErrorBox error={error} /> : null}
        <div className="flex justify-end gap-2">
          <button className="btn" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-danger" onClick={del} disabled={busy}>
            Delete
          </button>
        </div>
      </div>
    </Modal>
  );
}

export function ProjectsPanel({ selected, onSelect }: { selected: string | null; onSelect: (id: string | null) => void }) {
  const projects = useStore((s) => s.data.projects);
  const tasks = useStore((s) => s.data.tasks);
  const [editing, setEditing] = useState<Project | null | 'new'>(null);
  const [deleting, setDeleting] = useState<Project | null>(null);
  const list = Object.values(projects).sort((a, b) => a.name.localeCompare(b.name));
  const counts: Record<string, number> = {};
  for (const t of Object.values(tasks)) counts[t.project_id] = (counts[t.project_id] ?? 0) + 1;

  return (
    <aside aria-label="Projects" className="flex w-56 shrink-0 flex-col border-r border-line bg-void/30">
      <div className="flex items-center justify-between px-3 py-3">
        <span className="label">Projects</span>
        <button className="btn btn-sm" onClick={() => setEditing('new')} aria-label="Add project">
          <Plus className="size-3.5" aria-hidden /> Add
        </button>
      </div>
      <ul className="flex-1 overflow-auto px-2 pb-3">
        <li>
          <button
            className={`flex w-full items-center justify-between rounded-pixel px-2 py-1.5 text-left text-[13px] ${selected === null ? 'bg-raised text-phosphor' : 'text-muted hover:bg-panel'}`}
            onClick={() => onSelect(null)}
            aria-pressed={selected === null}
          >
            All projects
            <span className="tick-num text-xs text-faint">{Object.keys(tasks).length}</span>
          </button>
        </li>
        {list.map((p) => (
          <li key={p.id} className="group">
            <div className={`flex items-center rounded-pixel ${selected === p.id ? 'bg-raised' : 'hover:bg-panel'}`}>
              <button
                className={`flex min-w-0 flex-1 items-center gap-2 px-2 py-1.5 text-left text-[13px] ${selected === p.id ? 'text-phosphor' : 'text-ink'}`}
                onClick={() => onSelect(p.id)}
                aria-pressed={selected === p.id}
                title={p.path}
              >
                <FolderGit2 className="size-3.5 shrink-0" aria-hidden />
                <span className="truncate">{p.name}</span>
                <span className="tick-num ml-auto text-xs text-faint">{counts[p.id] ?? 0}</span>
              </button>
              <button className="btn btn-ghost btn-sm" aria-label={`Edit ${p.name}`} onClick={() => setEditing(p)}>
                <Pencil className="size-3" aria-hidden />
              </button>
              <button className="btn btn-ghost btn-sm btn-danger" aria-label={`Delete ${p.name}`} onClick={() => setDeleting(p)}>
                <Trash2 className="size-3" aria-hidden />
              </button>
            </div>
          </li>
        ))}
        {list.length === 0 && <li className="px-2 py-4 text-xs text-faint">No projects registered yet. Add one to start creating tasks.</li>}
      </ul>
      {editing !== null && (
        <ProjectForm
          key={editing === 'new' ? 'new' : editing.id}
          project={editing === 'new' ? null : editing}
          open
          onOpenChange={(o) => !o && setEditing(null)}
        />
      )}
      {deleting && (
        <DeleteProject
          project={deleting}
          onClose={() => {
            if (selected === deleting.id) onSelect(null);
            setDeleting(null);
          }}
        />
      )}
    </aside>
  );
}
