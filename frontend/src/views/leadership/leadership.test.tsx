import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../api/endpoints', () => ({
  api: {
    leadership: vi.fn(),
    putLeadership: vi.fn(),
    switchLeader: vi.fn(),
    consultAdvisor: vi.fn(),
    decisions: vi.fn(),
    exportDecision: vi.fn(),
  },
}));
import { ApiError } from '../../api/client';
import { api } from '../../api/endpoints';
import type { ConsultResponse, Decision, Leadership } from '../../api/types';
import { applyEvent, initialData } from '../../store/reducer';
import { useStore } from '../../store/store';
import { mkProject } from '../../test/fixtures';
import { LeadershipView } from '../LeadershipView';

const m = vi.mocked(api, true);

const lead = (o: Partial<Leadership> = {}): Leadership => ({
  leader: { provider: 'claude', model: 'claude-sonnet', tier: 'standard', source: 'default', reason: 'default', set_at: '2026-11-11T10:00:00Z' },
  advisor: null,
  advisor_effective: 'router',
  settings: {
    advisor_policy: 'on_request',
    fallback_policy: 'off',
    max_consultations_per_day: 10,
    advisor_after_failed_repairs: 2,
    skip_planning_for_small_tasks: true,
    max_leader_calls_per_task: 6,
    max_consultations_per_task: 2,
    max_parallel_workers: 3,
  },
  role_minimum_tier: { leader: 'standard', advisor: 'standard' },
  budgets: {
    consultations_today: { used: 3, limit: 10 },
    consultations_per_task: { limit: 2 },
    leader_calls_per_task: { limit: 6 },
    max_parallel_workers: { limit: 3 },
  },
  permissions: { approvals_required_for: ['git_push', 'delete_files'], auto_approve: ['read_files'], worker_permissions: {} },
  candidates: {
    leader: [
      { provider: 'claude', model: 'claude-sonnet', tier: 'standard', eligible: true, reasons: [] },
      { provider: 'codex', model: 'gpt-x', tier: 'advanced', eligible: true, reasons: [] },
      { provider: 'antigravity', model: 'gemini-flash', tier: 'fast', eligible: false, reasons: ['provider not installed'] },
    ],
    advisor: [{ provider: 'codex', model: 'gpt-x', tier: 'advanced', eligible: true, reasons: [] }],
  },
  assignment_paused: false,
  switch_in_progress: false,
  ...o,
});

const decision = (o: Partial<Decision> = {}): Decision => ({
  id: 'dec_1',
  project_id: 'prj_1',
  task_id: 'tsk_1',
  title: 'Use SQLite FTS5',
  context: 'We need local search.',
  options: ['FTS5', 'external engine'],
  decision: 'FTS5',
  rationale: 'No extra service.',
  evidence: [],
  advisor_run_id: null,
  kind: 'leader_decision',
  evidence_status: 'none',
  meta: {},
  created_at: '2026-11-11T10:00:00Z',
  ...o,
});

const consult = (o: Partial<ConsultResponse> = {}): ConsultResponse => ({
  decision_id: 'dec_9',
  run_id: 'run_9',
  trigger: 'user',
  provider: 'codex',
  model: 'gpt-x',
  independence: 'different_provider',
  evidence_status: 'none',
  result: { assessment: 'Looks fine.', recommended_option: 'Option A', risks: ['slow'], disagreements: [], confidence: 0.7, evidence_requested: ['a benchmark'] },
  budget: lead().budgets,
  ...o,
});

function renderView() {
  return render(
    <MemoryRouter>
      <LeadershipView />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  useStore.setState({ data: { ...initialData(), projects: { prj_1: mkProject() } } });
  m.leadership.mockResolvedValue(lead());
  m.decisions.mockResolvedValue([]);
});

describe('candidates', () => {
  it('lists ineligible models disabled with the backend reason, never hidden', async () => {
    renderView();
    const pick = await screen.findByRole('combobox', { name: 'Candidate leader model' });
    const bad = within(pick).getByRole('option', { name: /gemini-flash/ });
    expect(bad).toBeDisabled();
    expect(bad).toHaveTextContent('not eligible: provider not installed');
    expect(within(pick).getByRole('option', { name: /gpt-x/ })).toBeEnabled();
  });

  it('shows leader identity, tier, since and reason', async () => {
    renderView();
    const cur = await screen.findByTestId('current-leader');
    expect(cur).toHaveTextContent('claude / claude-sonnet');
    expect(cur).toHaveTextContent('standard');
    expect(screen.getByTestId('leader-reason')).toHaveTextContent('default');
  });
});

describe('safe switch', () => {
  async function openDialog() {
    renderView();
    const pick = await screen.findByRole('combobox', { name: 'Candidate leader model' });
    await userEvent.selectOptions(pick, 'codex::gpt-x');
    await userEvent.click(screen.getByRole('button', { name: /Switch leader\.\.\./ }));
    return screen.findByRole('dialog');
  }

  it('lists the backend steps and says running workers are unaffected, then reports success', async () => {
    m.switchLeader.mockResolvedValue({ status: 'switched', from: { provider: 'claude', model: 'claude-sonnet' }, to: { provider: 'codex', model: 'gpt-x' }, checkpoint_id: 'ckp_1', validated: true, skip_validation: false, attempts: 1, reason: 'manual switch (API)' });
    const dlg = await openDialog();
    const steps = within(dlg).getByRole('list', { name: 'Switch steps' });
    expect(within(steps).getAllByRole('listitem')).toHaveLength(6);
    expect(steps).toHaveTextContent(/Pause assigning/);
    expect(steps).toHaveTextContent(/checkpoint/);
    expect(steps).toHaveTextContent(/handoff/);
    expect(steps).toHaveTextContent(/validate/);
    expect(steps).toHaveTextContent(/Resume/);
    expect(dlg).toHaveTextContent(/Running workers are unaffected/);
    await userEvent.click(within(dlg).getByRole('button', { name: 'Switch leader' }));
    expect(m.switchLeader).toHaveBeenCalledWith({ provider: 'codex', model: 'gpt-x' });
    const res = await within(dlg).findByTestId('switch-result');
    expect(res).toHaveAttribute('data-status', 'switched');
    expect(res).toHaveTextContent('Validated by the new leader');
  });

  it('shows the rollback reason when validation fails (409 leader_switch_failed)', async () => {
    m.switchLeader.mockRejectedValue(
      new ApiError(409, 'leader_switch_failed', 'rolled back', {
        status: 'rolled_back',
        from: { provider: 'claude', model: 'claude-sonnet' },
        to: { provider: 'codex', model: 'gpt-x' },
        checkpoint_id: 'ckp_2',
        validated: false,
        skip_validation: false,
        attempts: 2,
        reason: 'missing task ids in the restated plan',
        leader: { provider: 'claude', model: 'claude-sonnet' },
      }),
    );
    const dlg = await openDialog();
    await userEvent.click(within(dlg).getByRole('button', { name: 'Switch leader' }));
    const res = await within(dlg).findByTestId('switch-result');
    expect(res).toHaveAttribute('data-status', 'rolled_back');
    expect(within(dlg).getByTestId('rollback-reason')).toHaveTextContent('missing task ids in the restated plan');
    expect(res).toHaveTextContent('claude / claude-sonnet is still the leader');
  });

  it('shows other errors as an alert and keeps the confirm available', async () => {
    m.switchLeader.mockRejectedValue(new ApiError(409, 'leader_changed_concurrently', 'the leader changed concurrently'));
    const dlg = await openDialog();
    await userEvent.click(within(dlg).getByRole('button', { name: 'Switch leader' }));
    expect(await within(dlg).findByRole('alert')).toHaveTextContent('the leader changed concurrently');
  });
});

describe('settings', () => {
  it('saves budgets with bounds validation and shows used/limit', async () => {
    m.putLeadership.mockResolvedValue(lead());
    renderView();
    const day = await screen.findByTestId('budget-max_consultations_per_day');
    expect(within(day).getByTestId('used-max_consultations_per_day')).toHaveTextContent('used 3 / limit 10');
    const input = within(day).getByLabelText('Consultations per day');
    await userEvent.clear(input);
    await userEvent.type(input, '5000');
    expect(screen.getByRole('button', { name: /Save fallback/ })).toBeDisabled();
    await userEvent.clear(input);
    await userEvent.type(input, '20');
    await userEvent.click(screen.getByRole('button', { name: /Save fallback/ }));
    expect(m.putLeadership).toHaveBeenCalledWith(expect.objectContaining({ max_consultations_per_day: 20, fallback_policy: 'off' }));
  });

  it('permissions are read-only lists', async () => {
    renderView();
    const sec = await screen.findByRole('region', { name: 'Permissions and approvals' });
    expect(within(sec).getByRole('list', { name: 'Requires approval' })).toHaveTextContent('git_push');
    expect(within(sec).queryByRole('checkbox')).toBeNull();
  });

  it('saves the advisor with its policy', async () => {
    m.putLeadership.mockResolvedValue(lead());
    renderView();
    const pick = await screen.findByRole('combobox', { name: 'Advisor model' });
    await userEvent.selectOptions(pick, 'codex::gpt-x');
    await userEvent.selectOptions(screen.getByLabelText('Activation policy'), 'auto');
    await userEvent.click(screen.getByRole('button', { name: 'Save advisor settings' }));
    expect(m.putLeadership).toHaveBeenCalledWith({ advisor_policy: 'auto', advisor_after_failed_repairs: 2, advisor: { provider: 'codex', model: 'gpt-x' } });
  });
});

describe('ask advisor', () => {
  it('shows the structured answer and the independence note (different provider)', async () => {
    m.consultAdvisor.mockResolvedValue(consult());
    renderView();
    await userEvent.type(await screen.findByLabelText('Problem'), 'Which database?');
    await userEvent.click(screen.getByRole('button', { name: 'Ask advisor' }));
    expect(m.consultAdvisor).toHaveBeenCalledWith({ task_id: null, problem: 'Which database?', options: [], constraints: [] });
    const ind = await screen.findByTestId('independence');
    expect(ind).toHaveAttribute('data-independence', 'different_provider');
    expect(ind).toHaveTextContent(/different provider/);
    expect(screen.getByTestId('recommended')).toHaveTextContent('Option A');
  });

  it('warns when the advisor is the same provider as the leader', async () => {
    m.consultAdvisor.mockResolvedValue(consult({ independence: 'same_provider', provider: 'claude' }));
    renderView();
    await userEvent.type(await screen.findByLabelText('Problem'), 'x');
    await userEvent.click(screen.getByRole('button', { name: 'Ask advisor' }));
    expect(await screen.findByTestId('independence')).toHaveTextContent(/same provider/);
  });

  it('explains a refusal with the backend reason', async () => {
    m.consultAdvisor.mockRejectedValue(new ApiError(429, 'advisor_budget_exhausted', 'daily budget used', { reason: 'budget_per_day' }));
    renderView();
    await userEvent.type(await screen.findByLabelText('Problem'), 'x');
    await userEvent.click(screen.getByRole('button', { name: 'Ask advisor' }));
    expect(await screen.findByTestId('refusal-reason')).toHaveTextContent('daily consultation budget');
  });
});

describe('decision records', () => {
  it('flags a decision without objective evidence and shows detail', async () => {
    m.decisions.mockResolvedValue([decision(), decision({ id: 'dec_2', title: 'Backed one', evidence_status: 'verified', evidence: [{ type: 'verification', id: 'ver_1' }] })]);
    renderView();
    const rows = await screen.findAllByTestId('decision-row');
    expect(within(rows[0]).getByTestId('evidence-status')).toHaveAttribute('data-status', 'none');
    expect(within(rows[1]).getByTestId('evidence-status')).toHaveAttribute('data-status', 'verified');
    await userEvent.click(rows[0]);
    const detail = await screen.findByTestId('decision-detail');
    expect(detail).toHaveTextContent('We need local search.');
    expect(within(detail).getByTestId('no-evidence-note')).toBeInTheDocument();
    await userEvent.click(rows[1]);
    const d2 = await screen.findByTestId('decision-detail');
    expect(within(d2).getByTestId('evidence-ref')).toHaveTextContent('ver_1');
    expect(within(d2).getByRole('link', { name: 'open the task verification' })).toHaveAttribute('href', '#/mission?task=tsk_1');
  });

  it('export is behind a confirm and reports the path', async () => {
    m.decisions.mockResolvedValue([decision()]);
    m.exportDecision.mockResolvedValue({ decision_id: 'dec_1', path: 'D:\\demo\\docs\\DECISIONS.md', appended: true });
    renderView();
    await userEvent.click(await screen.findByTestId('decision-row'));
    await userEvent.click(screen.getByRole('button', { name: /Export to project DECISIONS.md/ }));
    expect(m.exportDecision).not.toHaveBeenCalled();
    const dlg = await screen.findByRole('dialog');
    expect(dlg).toHaveTextContent('explicit request');
    await userEvent.click(within(dlg).getByRole('button', { name: 'Append to DECISIONS.md' }));
    expect(m.exportDecision).toHaveBeenCalledWith('dec_1');
    await waitFor(() => expect(within(dlg).getByTestId('export-result')).toHaveTextContent('DECISIONS.md'));
  });
});

describe('leader.changed event', () => {
  const ev = (payload: Record<string, unknown>) => ({ seq: 5, ts: '2026-11-11T10:00:00Z', type: 'leader.changed', payload });
  it('moves the top-bar leader to the new one on a switch', () => {
    const s = applyEvent(initialData(), ev({ from: { provider: 'claude', model: 'a' }, to: { provider: 'codex', model: 'b' }, status: 'switched' }));
    expect(s.leader).toEqual({ provider: 'codex', model: 'b' });
    expect(s.rev.leadership).toBe(1);
  });
  it('keeps the previous leader after a rollback', () => {
    const s = applyEvent(initialData(), ev({ from: { provider: 'claude', model: 'a' }, to: { provider: 'codex', model: 'b' }, status: 'rolled_back' }));
    expect(s.leader).toEqual({ provider: 'claude', model: 'a' });
  });
});
