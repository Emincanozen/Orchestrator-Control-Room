import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../api/endpoints', () => ({
  api: { createObjective: vi.fn(), objective: vi.fn(), objectives: vi.fn() },
}));
import { api } from '../../api/endpoints';
import type { Objective, ObjectiveTask } from '../../api/types';
import { initialData } from '../../store/reducer';
import { useStore } from '../../store/store';
import { mkProject, mkTask } from '../../test/fixtures';
import { ObjectiveDetail, ObjectiveForm, layoutGraph } from './Objectives';

const m = vi.mocked(api, true);

const t = (id: string, depends: string[] = [], state: ObjectiveTask['state'] = 'CREATED'): ObjectiveTask => ({ id, key: id, title: `Task ${id}`, state, task_type: 'coding', role: null, priority: 50, depends_on: depends });

const obj = (o: Partial<Objective> = {}): Objective => ({
  id: 'obj_1',
  project_id: 'prj_1',
  text: 'Build the exporter',
  status: 'planned',
  deliverables: ['CSV export'],
  acceptance_criteria: ['exports headers'],
  prerequisites: ['pandas installed'],
  error: null,
  unsafe_verify_commands: {},
  run_ids: [],
  tasks: [t('a'), t('b', ['a']), t('c', ['a']), t('d', ['b', 'c'])],
  task_counts: { CREATED: 4 },
  created_at: '2026-11-11T10:00:00Z',
  updated_at: '2026-11-11T10:00:00Z',
  ...o,
});

beforeEach(() => {
  vi.clearAllMocks();
  useStore.setState({ data: { ...initialData(), projects: { prj_1: mkProject() } } });
});

const renderDetail = (pollMs = 300) =>
  render(
    <MemoryRouter>
      <ObjectiveDetail objectiveId="obj_1" onClose={() => {}} onOpenTask={() => {}} pollMs={pollMs} />
    </MemoryRouter>,
  );

describe('layoutGraph', () => {
  it('layers tasks by longest dependency path', () => {
    const { nodes, layers } = layoutGraph([t('a'), t('b', ['a']), t('c', ['a']), t('d', ['b', 'c'])]);
    expect(layers).toBe(3);
    expect(Object.fromEntries(nodes.map((n) => [n.task.id, n.layer]))).toEqual({ a: 0, b: 1, c: 1, d: 2 });
  });
  it('does not loop on a cycle', () => {
    expect(() => layoutGraph([t('a', ['b']), t('b', ['a'])])).not.toThrow();
  });
});

describe('objective detail', () => {
  it('renders deliverables, prerequisites, criteria, the task graph with states and an indented task list', async () => {
    m.objective.mockResolvedValue(obj());
    renderDetail();
    expect(await screen.findByTestId('task-graph')).toBeInTheDocument();
    expect(screen.getByText('CSV export')).toBeInTheDocument();
    expect(screen.getByText('pandas installed')).toBeInTheDocument();
    expect(screen.getByText('exports headers')).toBeInTheDocument();
    expect(document.querySelectorAll('[data-node]')).toHaveLength(4);
    expect(document.querySelectorAll('[data-edge]')).toHaveLength(4);
    expect(document.querySelector('[data-edge="a>b"]')).not.toBeNull();
    const list = screen.getByRole('list', { name: 'Task list' });
    const items = within(list).getAllByTestId('objective-task');
    expect(items.map((i) => i.getAttribute('data-layer'))).toEqual(['0', '1', '1', '2']);
    expect(items[3]).toHaveTextContent('after: Task b, Task c');
    expect(screen.getByTestId('objective-status')).toHaveAttribute('data-status', 'planned');
  });

  it('uses live task states from the store', async () => {
    m.objective.mockResolvedValue(obj());
    useStore.setState({ data: { ...initialData(), projects: { prj_1: mkProject() }, tasks: { a: mkTask({ id: 'a', state: 'COMPLETED' }) } } });
    renderDetail();
    const list = await screen.findByRole('list', { name: 'Task list' });
    expect(within(list).getAllByTestId('objective-task')[0]).toHaveTextContent('Completed');
  });

  it('shows planning in progress and then the result when the poll sees it planned', async () => {
    m.objective.mockResolvedValueOnce(obj({ status: 'planning', tasks: [], deliverables: [] })).mockResolvedValue(obj());
    renderDetail();
    expect(await screen.findByTestId('objective-planning')).toBeInTheDocument();
    expect(await screen.findByTestId('task-graph', undefined, { timeout: 3000 })).toBeInTheDocument();
    expect(screen.queryByTestId('objective-planning')).toBeNull();
  });

  it('shows the backend reason when planning failed', async () => {
    m.objective.mockResolvedValue(obj({ status: 'failed', tasks: [], error: 'the leader returned a dependency cycle' }));
    renderDetail();
    expect(await screen.findByTestId('objective-failed')).toHaveTextContent('the leader returned a dependency cycle');
    expect(screen.queryByTestId('task-graph')).toBeNull();
  });
});

describe('unsafe verify commands', () => {
  it('lists proposed-but-not-adopted commands with the reason', async () => {
    m.objective.mockResolvedValue(obj({ unsafe_verify_commands: { b: [{ command: 'rm -rf build && make', reason: 'shell operators are not allowed' }] } }));
    renderDetail();
    const box = await screen.findByTestId('unsafe-verify');
    expect(box).toHaveTextContent('Proposed by the leader but not adopted');
    expect(box).toHaveTextContent('no automatic verification yet');
    expect(box).toHaveTextContent('rm -rf build && make');
    expect(box).toHaveTextContent('Task b');
  });
});

describe('objective form', () => {
  it('creates with autostart off by default and hands over the result', async () => {
    m.createObjective.mockResolvedValue(obj({ status: 'planning', tasks: [] }));
    const onCreated = vi.fn();
    render(
      <MemoryRouter>
        <ObjectiveForm defaultProjectId="prj_1" onClose={() => {}} onCreated={onCreated} />
      </MemoryRouter>,
    );
    expect(screen.getByRole('checkbox', { name: /Start the tasks/ })).not.toBeChecked();
    await userEvent.type(screen.getByLabelText('Objective'), 'Build the exporter');
    await userEvent.click(screen.getByRole('button', { name: 'Plan objective' }));
    expect(m.createObjective).toHaveBeenCalledWith('prj_1', { text: 'Build the exporter', autostart: false });
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
  });

  it('sends autostart when ticked', async () => {
    m.createObjective.mockResolvedValue(obj());
    render(
      <MemoryRouter>
        <ObjectiveForm defaultProjectId="prj_1" onClose={() => {}} onCreated={() => {}} />
      </MemoryRouter>,
    );
    await userEvent.type(screen.getByLabelText('Objective'), 'x');
    await userEvent.click(screen.getByRole('checkbox', { name: /Start the tasks/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Plan objective' }));
    expect(m.createObjective).toHaveBeenCalledWith('prj_1', { text: 'x', autostart: true });
  });
});
