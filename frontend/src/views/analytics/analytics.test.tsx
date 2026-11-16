import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../api/endpoints', () => ({
  api: {
    analyticsSummary: vi.fn(),
    analyticsTimeseries: vi.fn(),
    analyticsModels: vi.fn(),
    analyticsRouting: vi.fn(),
    analyticsAgents: vi.fn(),
    putAnalyticsAdaptiveSettings: vi.fn(),
  },
}));
import { api } from '../../api/endpoints';
import type { AnalyticsRouting, AnalyticsSummary, Metric } from '../../api/types';
import { fillGaps, tokenRows } from '../../lib/analytics';
import { initialData } from '../../store/reducer';
import { useStore } from '../../store/store';
import { AnalyticsView } from '../AnalyticsView';

const m = vi.mocked(api, true);

const miss = (why = 'x'): Metric<never> => ({ value: null as never, exactness: 'missing', source: why, n: 0 });
const meas = <V,>(value: V, n: number): Metric<V> => ({ value, exactness: 'measured', source: 'test', n });

const summary = (o: Partial<AnalyticsSummary['metrics']> = {}): AnalyticsSummary => ({
  scope: { project_id: null, provider: null, model: null, task_type: null, from: null, to: null },
  metrics: {
    tasks_completed: meas(2, 3),
    verified_completion_rate: meas(0.6667, 3),
    test_pass_rate: meas(0.5, 4),
    retries_total: meas(1, 3),
    retries_per_task: meas(0.3333, 3),
    repair_cycles_total: meas(1, 3),
    failure_frequency: meas({ test_failure: 1 }, 1),
    run_duration_seconds: meas({ p50: 2, p90: 4, mean: 2.5 }, 4),
    task_active_seconds: miss(),
    provider_model_switches: miss(),
    tokens: meas({ input: 120, output: 40, cached: 0, by_model: { 'mock/mock-model': { input: 120, output: 40, cached: null, runs: 1 } } }, 1),
    cost_usd_estimated: miss(),
    provider_availability: miss(),
    handoff_checkpoints: miss(),
    handoff_prompt_chars: miss(),
    ...o,
  },
});

const routing = (o: Partial<AnalyticsRouting> = {}): AnalyticsRouting => ({
  settings: { enabled: true, min_samples: 20, max_adjustment: 10, overrides: {}, bounds: { min_samples: [3, 500], max_adjustment: [0, 15] }, effective_cap_points: 5, note: 'cap note' },
  cells: [
    {
      provider: 'mock', model: 'mock-model', task_type: 'coding', runs: 4, verified_runs: 4, successes: 3, min_samples: 20, threshold_met: false,
      success_rate: meas(0.75, 4), adjustment: { points: 0, applied: false, explanation: 'only 4 verified runs, needs 20' },
    },
  ],
  applied_history: [],
  decisions_with_adaptive: 0,
  ...o,
});

function renderView() {
  return render(
    <MemoryRouter>
      <AnalyticsView />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  useStore.setState({ data: initialData() });
  m.analyticsSummary.mockResolvedValue(summary());
  m.analyticsTimeseries.mockResolvedValue({ metric: 'tasks_completed', points: [], exactness: 'measured', source: 's', n: 0 });
  m.analyticsModels.mockResolvedValue({
    models: [
      { provider: 'mock', model: 'mock-model', task_type: 'coding', runs: 4, verified_runs: 4, successes: 3, success_rate: meas(0.75, 4), avg_duration_seconds: meas(2, 4), p50_duration_seconds: meas(2, 4), avg_tokens: meas(160, 4), last_used: '2026-11-11T10:00:00Z' },
      { provider: 'claude', model: 'sonnet', task_type: 'docs', runs: 1, verified_runs: 0, successes: 0, success_rate: miss(), avg_duration_seconds: miss(), p50_duration_seconds: miss(), avg_tokens: miss(), last_used: null },
    ],
    definition: 'def',
  });
  m.analyticsRouting.mockResolvedValue(routing());
  m.analyticsAgents.mockResolvedValue({ agents: [] });
});

describe('pure helpers', () => {
  it('fillGaps leaves missing days as null, never zero, and sorts', () => {
    const out = fillGaps([
      { bucket: '2026-11-03', value: 5, n: 5 },
      { bucket: '2026-11-01', value: 2, n: 2 },
    ]);
    expect(out.map((p) => p.bucket)).toEqual(['2026-11-01', '2026-11-02', '2026-11-03']);
    expect(out[1].value).toBeNull();
    expect(out.every((p) => p.value !== 0)).toBe(true);
    expect(fillGaps([])).toEqual([]);
  });
  it('tokenRows splits provider and model', () => {
    expect(tokenRows({ 'claude/sonnet': { input: 1, output: 2, cached: null, runs: 1 }, mock: { input: 5, output: 5, cached: 0, runs: 1 } }).map((r) => [r.provider, r.model])).toEqual([
      ['mock', ''],
      ['claude', 'sonnet'],
    ]);
  });
});

describe('KPI tiles', () => {
  it('every tile shows exactness and n; missing metrics read "No data yet", never 0', async () => {
    renderView();
    await screen.findByTestId('kpi-tasks_completed');
    const tiles = screen.getAllByTestId(/^kpi-(?!value|n)/).filter((t) => t.hasAttribute('data-exactness'));
    expect(tiles.length).toBe(12);
    for (const t of tiles) {
      expect(within(t).getByTestId('kpi-n')).toBeInTheDocument();
      if (t.getAttribute('data-missing') === 'true') {
        expect(within(t).getByTestId('kpi-value')).toHaveTextContent('No data yet');
        expect(within(t).getByTestId('kpi-value').textContent).not.toMatch(/\b0\b/);
        expect(t).toHaveAttribute('data-exactness', 'missing');
        expect(within(t).getByTestId('kpi-n')).toHaveTextContent('n = 0');
      } else {
        expect(t.getAttribute('data-exactness')).toMatch(/measured|estimated/);
      }
    }
    expect(within(screen.getByTestId('kpi-cost_usd_estimated')).getByTestId('kpi-value')).toHaveTextContent('No data yet');
    expect(within(screen.getByTestId('kpi-tasks_completed')).getByTestId('kpi-value')).toHaveTextContent('2');
  });

  it('flags rates with a small n as low evidence and not the larger ones', async () => {
    m.analyticsSummary.mockResolvedValue(summary({ verified_completion_rate: meas(1, 2), test_pass_rate: meas(0.9, 40) }));
    renderView();
    const low = await screen.findByTestId('kpi-verified_completion_rate');
    expect(within(low).getByTestId('kpi-n')).toHaveTextContent('n = 2');
    expect(within(low).getByTestId('low-evidence')).toBeInTheDocument();
    expect(within(screen.getByTestId('kpi-test_pass_rate')).queryByTestId('low-evidence')).toBeNull();
  });

  it('estimated cost is labelled as not billed', async () => {
    m.analyticsSummary.mockResolvedValue(summary({ cost_usd_estimated: { value: 0.0123, exactness: 'estimated', source: 's', n: 2 } }));
    renderView();
    const tile = await screen.findByTestId('kpi-cost_usd_estimated');
    expect(tile).toHaveAttribute('data-exactness', 'estimated');
    expect(tile).toHaveTextContent('$0.0123');
    expect(await screen.findByTestId('cost-line')).toHaveTextContent('list-price equivalent, not billed');
  });
});

describe('charts', () => {
  it('time series with only gaps renders No data yet, and with data exposes gaps in the table alternative', async () => {
    m.analyticsTimeseries.mockResolvedValue({
      metric: 'tasks_completed',
      points: [
        { bucket: '2026-11-01', value: 2, n: 2 },
        { bucket: '2026-11-04', value: 1, n: 1 },
      ],
      exactness: 'measured',
      source: 's',
      n: 3,
    });
    renderView();
    const chart = await screen.findByTestId('timeseries-chart');
    expect(chart).toHaveAttribute('data-gaps', '2');
    expect(chart).toHaveAccessibleName(/2 days without data shown as gaps/);
    const table = screen.getByRole('table', { name: 'Time series values' });
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows).toHaveLength(4);
    expect(rows[1]).toHaveTextContent('2026-11-02');
    expect(rows[1]).toHaveTextContent('No data yet');
    expect(rows[1]).not.toHaveTextContent(/\b0\b/);
  });

  it('shows No data yet for failures, tokens and durations when missing', async () => {
    m.analyticsSummary.mockResolvedValue(summary({ failure_frequency: miss(), tokens: miss(), run_duration_seconds: miss() }));
    renderView();
    for (const id of ['failures', 'tokens', 'run-duration']) {
      const card = await screen.findByTestId(`chart-${id}`);
      expect(within(card).getByTestId('no-data')).toHaveTextContent('No data yet');
    }
  });

  it('changing the metric refetches the series', async () => {
    renderView();
    await userEvent.selectOptions(await screen.findByLabelText('Metric'), 'test_pass_rate');
    await waitFor(() => expect(m.analyticsTimeseries).toHaveBeenLastCalledWith('test_pass_rate', {}));
  });

  it('filters are sent to the summary', async () => {
    renderView();
    await userEvent.selectOptions(await screen.findByLabelText('Task type'), 'coding');
    await waitFor(() => expect(m.analyticsSummary).toHaveBeenLastCalledWith({ task_type: 'coding' }));
  });
});

describe('model table', () => {
  it('shows success rate with n, "No data yet" for none, a threshold marker, and sorts', async () => {
    renderView();
    const rows = await screen.findAllByTestId('model-row');
    expect(rows).toHaveLength(2);
    const mock = rows.find((r) => r.textContent?.includes('mock-model'))!;
    expect(within(mock).getByTestId('success-rate')).toHaveTextContent('75% (n = 4)');
    expect(within(mock).getByTestId('threshold')).toHaveAttribute('data-met', 'false');
    const claude = rows.find((r) => r.textContent?.includes('sonnet'))!;
    expect(within(claude).getByTestId('success-rate')).toHaveTextContent('No data yet');
    await userEvent.click(screen.getByRole('button', { name: /^Provider/ }));
    expect(screen.getByRole('columnheader', { name: /^Provider/ })).toHaveAttribute('aria-sort', 'descending');
    expect(screen.getAllByTestId('model-row')[0]).toHaveTextContent('mock');
  });
});

describe('adaptive routing', () => {
  it('validates bounds before saving and sends the values', async () => {
    m.putAnalyticsAdaptiveSettings.mockResolvedValue(routing().settings);
    renderView();
    const min = await screen.findByLabelText('Minimum verified runs');
    const save = screen.getByRole('button', { name: 'Save adaptive settings' });
    expect(save).toBeDisabled();
    await userEvent.clear(min);
    await userEvent.type(min, '2');
    expect(save).toBeDisabled();
    expect(screen.getByText('Whole number from 3 to 500.')).toBeInTheDocument();
    await userEvent.clear(min);
    await userEvent.type(min, '30');
    const max = screen.getByLabelText('Maximum adjustment (points)');
    await userEvent.clear(max);
    await userEvent.type(max, '20');
    expect(save).toBeDisabled();
    await userEvent.clear(max);
    await userEvent.type(max, '7.5');
    expect(save).toBeEnabled();
    await userEvent.click(save);
    expect(m.putAnalyticsAdaptiveSettings).toHaveBeenCalledWith({ enabled: true, min_samples: 30, max_adjustment: 7.5 });
  });

  it('shows per-cell adjustment explanation and an honest empty history', async () => {
    renderView();
    const cell = await screen.findByTestId('adaptive-cell');
    expect(cell).toHaveTextContent('only 4 verified runs, needs 20');
    expect(cell).toHaveTextContent('none applied');
    expect(await screen.findByTestId('no-adaptive-history')).toHaveTextContent('No data yet');
  });

  it('lists decisions where adaptive routing was applied', async () => {
    m.analyticsRouting.mockResolvedValue(routing({ decisions_with_adaptive: 1, applied_history: [{ id: 'rtd_1', task_id: 'tsk_1', run_id: null, phase: 'execute', provider: 'mock', model: 'mock-model', created_at: '2026-11-11T10:00:00Z', adaptive_reasons: ['adaptive +2.5: 25 verified runs'] }] }));
    renderView();
    const h = await screen.findByTestId('adaptive-history');
    expect(h).toHaveTextContent('adaptive +2.5');
    expect(within(h).getByRole('link', { name: 'routing decision' })).toHaveAttribute('href', '#/routing?task=tsk_1');
  });
});

describe('agent history', () => {
  it('shows No data yet for agents without runs, not zero', async () => {
    m.analyticsAgents.mockResolvedValue({
      agents: [{ agent_id: 'a1', name: 'Ada', role: 'backend', room: null, provider: null, model: null, activity: 'idle', runs: miss(), succeeded: miss(), failed: miss(), working_seconds: miss(), last_run_at: null }],
    });
    renderView();
    const row = await screen.findByTestId('agent-row');
    expect(within(row).getByTestId('agent-runs')).toHaveTextContent('No data yet');
  });
});
