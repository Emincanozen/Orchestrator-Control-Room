import { act, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../api/endpoints', () => ({ api: { events: vi.fn() } }));
import { api } from '../../api/endpoints';
import type { ApiEvent } from '../../api/types';
import { initialData } from '../../store/reducer';
import { useStore } from '../../store/store';
import { mkProject } from '../../test/fixtures';
import { LogsView } from '../LogsView';
import { EMPTY_FILTER, filterEvents, windowRange } from './filter';

const m = vi.mocked(api, true);
const ev = (seq: number, type: string, o: Partial<ApiEvent> = {}): ApiEvent => ({ seq, ts: '2026-11-11T10:00:00Z', type, payload: {}, ...o });

describe('filterEvents', () => {
  const evs = [
    ev(1, 'task.created', { task_id: 'tsk_a', project_id: 'p1', payload: { title: 'Alpha' } }),
    ev(2, 'run.output', { task_id: 'tsk_a', project_id: 'p1', agent_id: 'ag_1', payload: { text: 'hello world' } }),
    ev(3, 'task.state_changed', { task_id: 'tsk_b', project_id: 'p2', payload: { to: 'FAILED' } }),
  ];
  it('returns newest first and filters by type prefix', () => {
    expect(filterEvents(evs, { ...EMPTY_FILTER, type: 'task.' }).map((e) => e.seq)).toEqual([3, 1]);
    expect(filterEvents(evs, EMPTY_FILTER).map((e) => e.seq)).toEqual([3, 2, 1]);
  });
  it('filters by project, task, agent and payload text', () => {
    expect(filterEvents(evs, { ...EMPTY_FILTER, project: 'p2' }).map((e) => e.seq)).toEqual([3]);
    expect(filterEvents(evs, { ...EMPTY_FILTER, task: 'TSK_A' }).map((e) => e.seq)).toEqual([2, 1]);
    expect(filterEvents(evs, { ...EMPTY_FILTER, agent: 'ag_1' }).map((e) => e.seq)).toEqual([2]);
    expect(filterEvents(evs, { ...EMPTY_FILTER, text: 'hello' }).map((e) => e.seq)).toEqual([2]);
    expect(filterEvents(evs, { ...EMPTY_FILTER, text: 'failed' }).map((e) => e.seq)).toEqual([3]);
  });
});

describe('windowRange', () => {
  it('renders a bounded window of a long list', () => {
    const { start, end } = windowRange(22 * 5000, 440, 22, 10000);
    expect(end - start).toBeLessThan(50);
    expect(start).toBeGreaterThan(4900);
  });
});

describe('LogsView', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useStore.setState({ data: { ...initialData(), projects: { p1: mkProject({ id: 'p1', name: 'One' }) }, lastSeq: 3 } });
  });
  const renderView = () =>
    render(
      <MemoryRouter>
        <LogsView />
      </MemoryRouter>,
    );

  it('loads the backlog, filters it and shows the payload as text with a jump-to-task link', async () => {
    m.events.mockResolvedValue([
      ev(1, 'task.created', { task_id: 'tsk_a', payload: { title: '<b>x</b>' } }),
      ev(2, 'run.output', { task_id: 'tsk_a', payload: { text: 'out' } }),
      ev(3, 'agent.activity_changed', { agent_id: 'ag_1' }),
    ]);
    renderView();
    const rows = await screen.findAllByTestId('log-row');
    expect(rows.map((r) => r.getAttribute('data-seq'))).toEqual(['3', '2', '1']);
    await userEvent.type(screen.getByLabelText('Event type prefix'), 'task.');
    expect(screen.getAllByTestId('log-row').map((r) => r.getAttribute('data-seq'))).toEqual(['1']);
    await userEvent.click(screen.getByTestId('log-row'));
    const detail = screen.getByTestId('event-detail');
    expect(within(detail).getByTestId('payload')).toHaveTextContent('"title": "<b>x</b>"');
    expect(detail.querySelector('b')).toBeNull();
    expect(within(detail).getByTestId('jump-task')).toHaveAttribute('href', '#/mission?task=tsk_a');
  });

  it('pauses the live stream and merges the buffered events on resume', async () => {
    m.events.mockResolvedValue([ev(1, 'task.created')]);
    renderView();
    await screen.findAllByTestId('log-row');
    const { subscribeEvents } = await import('../../store/store');
    expect(subscribeEvents).toBeTypeOf('function');
    await userEvent.click(screen.getByTestId('pause-toggle'));
    await act(async () => {
      useStore.getState().ingest([ev(4, 'run.output', { task_id: 'tsk_z' })]);
    });
    expect(screen.getAllByTestId('log-row')).toHaveLength(1);
    expect(screen.getByTestId('pause-toggle')).toHaveTextContent('Resume (1 new)');
    await userEvent.click(screen.getByTestId('pause-toggle'));
    expect(screen.getAllByTestId('log-row').map((r) => r.getAttribute('data-seq'))).toEqual(['4', '1']);
  });

  it('keeps thousands of events cheap by rendering a window only', async () => {
    m.events.mockResolvedValue(Array.from({ length: 4000 }, (_, i) => ev(i + 1, 'run.output', { task_id: 'tsk_a' })));
    renderView();
    const log = await screen.findByTestId('event-log');
    expect(log).toHaveAttribute('data-rows', '4000');
    expect(screen.getAllByTestId('log-row').length).toBeLessThan(80);
  });
});
