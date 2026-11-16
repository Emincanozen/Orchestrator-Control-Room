import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mkApproval, mkProject, mkTask } from '../../test/fixtures';
import { applySnapshot, initialData } from '../../store/reducer';
import { useStore } from '../../store/store';
import type { Approval, Task } from '../../api/types';

vi.mock('../../api/endpoints', () => ({
  api: {
    taskAction: vi.fn(),
    task: vi.fn(),
    taskTransitions: vi.fn(),
    createCheckpoint: vi.fn(),
    restoreCheckpoint: vi.fn(),
    taskArtifact: vi.fn(),
    taskRuns: vi.fn(),
    taskCheckpoints: vi.fn(),
    taskDiff: vi.fn(),
    taskArtifacts: vi.fn(),
    taskVerification: vi.fn(),
    events: vi.fn(),
    resolveApproval: vi.fn(),
    models: vi.fn(),
  },
}));

import { api } from '../../api/endpoints';
import { ApiError } from '../../api/client';
import { ApprovalsView } from '../ApprovalsView';
import { TaskBoard, filterTasks } from './TaskBoard';
import { TaskDetail } from './TaskDetail';

const m = vi.mocked(api, true);

function load(tasks: Task[], approvals: Approval[] = []) {
  useStore.setState({
    data: applySnapshot(initialData(), { projects: [mkProject(), mkProject({ id: 'prj_2', name: 'Other' })], tasks, agents: [], approvals, lastSeq: 0, }),
    unavailable: { agents: false, approvals: false },
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  m.task.mockResolvedValue(mkTask());
  m.events.mockResolvedValue([]);
  m.taskTransitions.mockResolvedValue([]);
  m.taskRuns.mockResolvedValue([]);
  m.taskCheckpoints.mockResolvedValue([]);
  m.taskArtifacts.mockResolvedValue({ root: 'D:\\wt', files: [] });
  m.taskVerification.mockResolvedValue([]);
  m.taskDiff.mockResolvedValue({ available: true, diff: '', changed_files: [] });
  m.models.mockResolvedValue([]);
});

describe('filterTasks', () => {
  const ts = [mkTask({ id: 'a', title: 'Alpha parser' }), mkTask({ id: 'b', title: 'Beta', state: 'RUNNING', project_id: 'prj_2' })];
  it('filters by project, state and search', () => {
    expect(filterTasks(ts, { projectId: 'prj_2', state: 'ALL', search: '' }).map((t) => t.id)).toEqual(['b']);
    expect(filterTasks(ts, { projectId: null, state: 'RUNNING', search: '' }).map((t) => t.id)).toEqual(['b']);
    expect(filterTasks(ts, { projectId: null, state: 'ALL', search: 'parser' }).map((t) => t.id)).toEqual(['a']);
  });
});

describe('TaskBoard', () => {
  it('groups tasks into the 13 state columns', () => {
    const tasks = [mkTask({ id: 'a', title: 'One', state: 'CREATED' }), mkTask({ id: 'b', title: 'Two', state: 'RUNNING' }), mkTask({ id: 'c', title: 'Three', state: 'FAILED' })];
    load(tasks);
    render(<TaskBoard tasks={tasks} onOpen={() => undefined} hideEmpty={false} />);
    expect(screen.getAllByRole('listitem', { name: /tasks$/ })).toHaveLength(13);
    expect(within(screen.getByTestId('col-RUNNING')).getByText('Two')).toBeInTheDocument();
    expect(within(screen.getByTestId('col-CREATED')).getByText('One')).toBeInTheDocument();
    expect(within(screen.getByTestId('col-FAILED')).getByText('Three')).toBeInTheDocument();
  });

  it('hides empty columns on request', () => {
    const tasks = [mkTask({ id: 'a', state: 'CREATED' })];
    load(tasks);
    render(<TaskBoard tasks={tasks} onOpen={() => undefined} hideEmpty />);
    expect(screen.getAllByRole('listitem', { name: /tasks$/ })).toHaveLength(1);
  });

  it('card actions follow the state machine and call the API', async () => {
    const tasks = [mkTask({ id: 'a', title: 'One', state: 'CREATED' }), mkTask({ id: 'b', title: 'Two', state: 'COMPLETED' })];
    load(tasks);
    m.taskAction.mockResolvedValue(mkTask({ id: 'a', title: 'One', state: 'QUEUED' }));
    render(<TaskBoard tasks={tasks} onOpen={() => undefined} hideEmpty={false} />);
    expect(screen.queryByRole('button', { name: 'Start task Two' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Pause task One' })).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'Start task One' }));
    expect(m.taskAction).toHaveBeenCalledWith('a', 'start');
    await waitFor(() => expect(useStore.getState().data.tasks.a.state).toBe('QUEUED'));
  });

  it('opens a task when its title is clicked', async () => {
    const tasks = [mkTask({ id: 'a', title: 'One' })];
    load(tasks);
    const onOpen = vi.fn();
    render(<TaskBoard tasks={tasks} onOpen={onOpen} hideEmpty={false} />);
    await userEvent.click(screen.getByRole('button', { name: 'Open task One' }));
    expect(onOpen).toHaveBeenCalledWith('a');
  });
});

describe('TaskDetail', () => {
  const running = mkTask({ id: 'tsk_1', title: 'Build it', state: 'RUNNING', provider: 'claude', model: 'claude-sonnet', pending_steps: ['write tests'], description: 'Do the thing' });

  it('shows overview with recorded values and unknowns, and gates actions', async () => {
    load([running]);
    m.task.mockResolvedValue(running);
    render(<TaskDetail taskId="tsk_1" onClose={() => undefined} />);
    expect(await screen.findByText('Do the thing')).toBeInTheDocument();
    expect(screen.getByText('write tests')).toBeInTheDocument();
    expect(screen.getByText('claude / claude-sonnet')).toBeInTheDocument();
    expect(screen.getByText(/unknown \(no measured usage recorded\)/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start task Build it' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Resume task Build it' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Retry task Build it' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Pause task Build it' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Cancel task Build it' })).toBeEnabled();
  });

  it('renders the unified diff with add/del classification', async () => {
    load([running]);
    m.task.mockResolvedValue(running);
    m.taskDiff.mockResolvedValue({ available: true, changed_files: ['x'], diff: 'diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n ctx' });
    render(<TaskDetail taskId="tsk_1" onClose={() => undefined} />);
    await userEvent.click(await screen.findByRole('tab', { name: 'Diff' }));
    const diff = await screen.findByTestId('diff');
    expect(diff.querySelector('[data-kind="add"]')?.textContent).toBe('+new');
    expect(diff.querySelector('[data-kind="del"]')?.textContent).toBe('-old');
    expect(diff.querySelector('[data-kind="hunk"]')).not.toBeNull();
  });

  it('shows a not-available state when an endpoint is missing (404)', async () => {
    load([running]);
    m.task.mockResolvedValue(running);
    m.taskRuns.mockRejectedValue(new ApiError(404, 'not_found', 'no'));
    render(<TaskDetail taskId="tsk_1" onClose={() => undefined} />);
    await userEvent.click(await screen.findByRole('tab', { name: 'Runs' }));
    expect(await screen.findByTestId('unavailable')).toHaveTextContent('not available yet');
  });

  it('a 404 on the workspace endpoints means "no workspace yet", not "endpoint missing"', async () => {
    load([running]);
    m.task.mockResolvedValue(running);
    m.taskArtifacts.mockRejectedValue(new ApiError(404, 'not_found', 'task has no workspace yet'));
    render(<TaskDetail taskId="tsk_1" onClose={() => undefined} />);
    await userEvent.click(await screen.findByRole('tab', { name: 'Artifacts' }));
    expect(await screen.findByText('No workspace yet')).toBeInTheDocument();
    expect(screen.queryByTestId('unavailable')).toBeNull();
  });

  it('lists changed files and shows a file viewer', async () => {
    load([running]);
    m.task.mockResolvedValue(running);
    m.taskArtifacts.mockResolvedValue({ root: 'D:\\wt', files: [{ path: 'out/result.txt', exists: true, size: 5 }] });
    m.taskArtifact.mockResolvedValue({ path: 'out/result.txt', size: 5, content: 'hello' });
    render(<TaskDetail taskId="tsk_1" onClose={() => undefined} />);
    await userEvent.click(await screen.findByRole('tab', { name: 'Artifacts' }));
    await userEvent.click(await screen.findByRole('button', { name: /out\/result\.txt/ }));
    expect(await screen.findByTestId('artifact-content')).toHaveTextContent('hello');
    expect(m.taskArtifact).toHaveBeenCalledWith('tsk_1', 'out/result.txt');
  });

  it('shows the transitions timeline from /transitions', async () => {
    load([running]);
    m.task.mockResolvedValue(running);
    m.taskTransitions.mockResolvedValue([
      { id: 1, task_id: 'tsk_1', from_state: 'CREATED', to_state: 'QUEUED', reason: 'started by user', actor: 'user', ts: '2026-11-11T10:00:00Z' },
      { id: 2, task_id: 'tsk_1', from_state: 'QUEUED', to_state: 'RUNNING', reason: null, actor: 'scheduler', ts: '2026-11-11T10:00:02Z' },
    ]);
    render(<TaskDetail taskId="tsk_1" onClose={() => undefined} />);
    await userEvent.click(await screen.findByRole('tab', { name: 'Timeline' }));
    const tl = await screen.findByTestId('timeline');
    expect(within(tl).getAllByRole('listitem')).toHaveLength(2);
    expect(tl).toHaveTextContent('started by user');
  });

  it('creates a manual checkpoint and restores one only after confirming', async () => {
    const paused = mkTask({ id: 'tsk_1', title: 'Build it', state: 'PAUSED' });
    load([paused]);
    m.task.mockResolvedValue(paused);
    const cp = { id: 'chk_1', task_id: 'tsk_1', run_id: null, kind: 'manual' as const, summary: { label: 'before refactor' }, git_ref: 'abc123', created_at: '2026-11-11T10:00:00Z' };
    m.taskCheckpoints.mockResolvedValueOnce([]).mockResolvedValue([cp]);
    m.createCheckpoint.mockResolvedValue(cp);
    m.restoreCheckpoint.mockResolvedValue(cp);
    render(<TaskDetail taskId="tsk_1" onClose={() => undefined} />);
    await userEvent.click(await screen.findByRole('tab', { name: 'Checkpoints' }));
    await userEvent.type(await screen.findByLabelText('Label (optional)'), 'before refactor');
    await userEvent.click(screen.getByRole('button', { name: /Create checkpoint/ }));
    expect(m.createCheckpoint).toHaveBeenCalledWith('tsk_1', 'before refactor');
    const restoreBtn = await screen.findByRole('button', { name: 'Restore checkpoint chk_1' });
    await userEvent.click(restoreBtn);
    expect(m.restoreCheckpoint).not.toHaveBeenCalled(); // confirm dialog first
    await userEvent.click(await screen.findByRole('button', { name: 'Restore worktree' }));
    expect(m.restoreCheckpoint).toHaveBeenCalledWith('tsk_1', 'chk_1');
  });

  it('disables checkpoint actions while the task is active', async () => {
    load([running]);
    m.task.mockResolvedValue(running);
    render(<TaskDetail taskId="tsk_1" onClose={() => undefined} />);
    await userEvent.click(await screen.findByRole('tab', { name: 'Checkpoints' }));
    expect(await screen.findByRole('button', { name: /Create checkpoint/ })).toBeDisabled();
  });

  it('shows verification checks with exit codes', async () => {
    load([running]);
    m.task.mockResolvedValue(running);
    m.taskVerification.mockResolvedValue([
      {
        id: 'ver_1',
        task_id: 'tsk_1',
        run_id: null,
        status: 'failed',
        started_at: '2026-11-11T10:00:00Z',
        finished_at: null,
        checks: [{ name: 'unit', kind: 'command', argv: ['npm', 'test'], path: null, passed: false, exit_code: 2, timed_out: false, duration_seconds: 3, output: 'boom', error: null }],
      },
    ]);
    render(<TaskDetail taskId="tsk_1" onClose={() => undefined} />);
    await userEvent.click(await screen.findByRole('tab', { name: 'Verification' }));
    expect(await screen.findByText('exit 2')).toBeInTheDocument();
    expect(screen.getByText('boom')).toBeInTheDocument();
  });

  it('live log streams events from the store', async () => {
    load([running]);
    m.task.mockResolvedValue(running);
    render(<TaskDetail taskId="tsk_1" onClose={() => undefined} />);
    await userEvent.click(await screen.findByRole('tab', { name: 'Live log' }));
    expect(await screen.findByText(/No output recorded/)).toBeInTheDocument();
    act(() => {
      useStore.getState().ingest([{ seq: 1, ts: '2026-11-11T10:00:01Z', type: 'run.output', task_id: 'tsk_1', payload: { text: 'compiling...' } }]);
    });
    expect(await screen.findByText(/compiling\.\.\./)).toBeInTheDocument();
  });
});

describe('ApprovalsView', () => {
  it('lists pending approvals and approves with a note', async () => {
    load([mkTask()], [mkApproval()]);
    m.resolveApproval.mockResolvedValue(mkApproval({ status: 'approved', note: 'ok' }));
    render(
      <MemoryRouter>
        <ApprovalsView />
      </MemoryRouter>,
    );
    expect(screen.getByText('Force push branch orch/tsk_1')).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText('Note for approval apr_1'), 'ok');
    await userEvent.click(screen.getByRole('button', { name: /Approve/ }));
    expect(m.resolveApproval).toHaveBeenCalledWith('apr_1', true, 'ok');
    await waitFor(() => expect(screen.getByTestId('empty')).toHaveTextContent('Nothing waiting for approval'));
  });

  it('denies and surfaces API errors', async () => {
    load([mkTask()], [mkApproval()]);
    m.resolveApproval.mockRejectedValue(new ApiError(409, 'conflict', 'Already resolved'));
    render(
      <MemoryRouter>
        <ApprovalsView />
      </MemoryRouter>,
    );
    await userEvent.click(screen.getByRole('button', { name: /Deny/ }));
    expect(m.resolveApproval).toHaveBeenCalledWith('apr_1', false, '');
    expect(await screen.findByRole('alert')).toHaveTextContent('Already resolved');
  });

  it('shows the not-available state when the endpoint is missing', () => {
    load([]);
    useStore.setState({ unavailable: { agents: false, approvals: true } });
    render(<ApprovalsView />);
    expect(screen.getByTestId('unavailable')).toBeInTheDocument();
  });
});
