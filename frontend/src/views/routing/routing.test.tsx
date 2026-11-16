import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../api/endpoints', () => ({
  api: {
    providers: vi.fn(),
    models: vi.fn(),
    routingDecisions: vi.fn(),
    routingPreview: vi.fn(),
    routingPreferences: vi.fn(),
    putRoutingPreferences: vi.fn(),
  },
}));
import { api } from '../../api/endpoints';
import type { RoutingDecision, RoutingPreferences, RoutingPreview } from '../../api/types';
import { RoutingView } from '../RoutingView';

const m = vi.mocked(api, true);

const prefs = (o: Partial<RoutingPreferences> = {}): RoutingPreferences => ({
  provider_order: ['claude', 'codex', 'antigravity'],
  disabled_providers: [],
  disabled_models: [],
  defaults: { provider_order: ['claude', 'codex', 'antigravity'], disabled_providers: [], disabled_models: [] },
  overridden: [],
  ...o,
});

const cls = { task_type: 'coding', complexity: 'medium', risk: 'low', required_capabilities: ['coding', 'ui_implementation'], signals: ['mentions a React component'] };

const decision: RoutingDecision = {
  id: 'rtd_1',
  task_id: 'tsk_1',
  run_id: 'run_1',
  phase: 'execute',
  chosen_provider: 'claude',
  chosen_model: 'claude-sonnet',
  candidates: [
    { rank: 1, provider: 'claude', model: 'claude-sonnet', tier: 'standard', score: 80, components: { tier_fit: 40, user_preference: 12.5, context_continuity: 0, usage_headroom: 9, historical_success: 2.5 }, adjustment: 2.5, adjustment_explanation: '25 verified runs, 92% success: +2.5 points', usage_state: 'AVAILABLE' },
    { rank: 2, provider: 'codex', model: 'gpt-x', tier: 'advanced', score: 61.2, components: { tier_fit: 26.7, user_preference: 6, context_continuity: 0, usage_headroom: 9, historical_success: 0 }, usage_state: 'UNKNOWN' },
  ],
  reasons: ['best tier fit for a medium coding task'],
  rule_ids: ['task_type:coding.min_tier', 'complexity:medium'],
  adaptive_applied: true,
  detail: {
    classification: cls,
    decision: {
      provider: 'claude',
      model: 'claude-sonnet',
      min_tier: 'standard',
      target_tier: 'standard',
      rejected: [
        { provider: 'antigravity', model: 'gemini-flash', filter: 'not_installed', filters: ['not_installed', 'below_min_tier'], detail: 'provider not installed or no status reported; tier fast < required standard' },
      ],
    },
    fallback: { action: 'switch', category: 'quota', reason: 'claude is rate limited' },
  },
  created_at: '2026-11-11T10:00:00Z',
};

function renderView(path = '/routing') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <RoutingView />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  m.providers.mockResolvedValue([]);
  m.models.mockResolvedValue([]);
  m.routingPreferences.mockResolvedValue(prefs());
  m.routingDecisions.mockResolvedValue([]);
});

describe('routing preferences', () => {
  it('reorders providers with keyboard-accessible buttons and saves only provider_order', async () => {
    m.putRoutingPreferences.mockResolvedValue(prefs({ provider_order: ['codex', 'claude', 'antigravity'], overridden: ['provider_order'] }));
    renderView();
    const list = await screen.findByRole('list', { name: 'Provider order' });
    const save = screen.getByRole('button', { name: 'Save Provider order' });
    expect(save).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Move claude up' })).toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: 'Move codex up' }));
    expect(within(list).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      expect.stringContaining('codex'),
      expect.stringContaining('claude'),
      expect.stringContaining('antigravity'),
    ]);
    // focus stays on the moved item's button (now disabled at the top: falls to its Down button)
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Move codex down' }));
    await userEvent.click(save);
    expect(m.putRoutingPreferences).toHaveBeenCalledWith({ provider_order: ['codex', 'claude', 'antigravity'] });
    const section = await screen.findByRole('region', { name: 'Provider order' });
    await waitFor(() => expect(section).toHaveAttribute('data-overridden', 'true'));
    expect(within(section).getByText('overridden')).toBeInTheDocument();
  });

  it('shows default vs overridden and resets with an explicit null', async () => {
    m.routingPreferences.mockResolvedValue(prefs({ disabled_providers: ['codex'], overridden: ['disabled_providers'] }));
    m.putRoutingPreferences.mockResolvedValue(prefs());
    renderView();
    const section = await screen.findByRole('region', { name: 'Disabled providers' });
    expect(within(section).getByText('overridden')).toBeInTheDocument();
    expect(within(section).getByText(/Default: none/)).toBeInTheDocument();
    const order = screen.getByRole('region', { name: 'Provider order' });
    expect(within(order).getByText('default')).toBeInTheDocument();
    expect(within(order).getByRole('button', { name: 'Reset Provider order to default' })).toBeDisabled();
    await userEvent.click(within(section).getByRole('button', { name: 'Reset Disabled providers to default' }));
    expect(m.putRoutingPreferences).toHaveBeenCalledWith({ disabled_providers: null });
    await waitFor(() => expect(screen.getByRole('region', { name: 'Disabled providers' })).toHaveAttribute('data-overridden', 'false'));
  });

  it('disables a provider and sends the full list', async () => {
    m.providers.mockResolvedValue([{ provider: 'codex' }, { provider: 'claude' }] as never);
    m.putRoutingPreferences.mockResolvedValue(prefs({ disabled_providers: ['codex'], overridden: ['disabled_providers'] }));
    renderView();
    const section = await screen.findByRole('region', { name: 'Disabled providers' });
    await userEvent.click(within(section).getByLabelText('Disable codex'));
    await userEvent.click(within(section).getByRole('button', { name: 'Save Disabled providers' }));
    expect(m.putRoutingPreferences).toHaveBeenCalledWith({ disabled_providers: ['codex'] });
  });

  it('shows the backend error inline', async () => {
    const { ApiError } = await import('../../api/client');
    m.providers.mockResolvedValue([{ provider: 'codex' }, { provider: 'claude' }] as never);
    m.putRoutingPreferences.mockRejectedValue(new ApiError(422, 'validation_failed', 'provider_order must contain non-empty strings'));
    renderView();
    const section = await screen.findByRole('region', { name: 'Disabled providers' });
    await userEvent.click(within(section).getByLabelText('Disable claude'));
    await userEvent.click(within(section).getByRole('button', { name: 'Save Disabled providers' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('non-empty strings');
  });
});

describe('routing decisions ("Why this model?")', () => {
  it('expands to classification, ranked candidates with score breakdown, rejected filters, rules, adaptive and fallback', async () => {
    m.routingDecisions.mockResolvedValue([decision]);
    renderView();
    const row = await screen.findByTestId('decision');
    expect(screen.queryByTestId('explanation')).toBeNull();
    await userEvent.click(within(row).getByRole('button', { expanded: false }));
    const x = await within(row).findByTestId('explanation');
    expect(within(x).getByTestId('cls-type')).toHaveTextContent('coding');
    expect(within(x).getByText('ui_implementation')).toBeInTheDocument();
    expect(within(x).getByText('mentions a React component')).toBeInTheDocument();
    const cands = within(x).getAllByTestId('candidate');
    expect(cands).toHaveLength(2);
    expect(within(cands[0]).getByRole('img')).toHaveAccessibleName(/Tier fit 40\.0, Preference 12\.5/);
    expect(cands[0]).toHaveTextContent('80.0');
    expect(cands[1]).toHaveTextContent('61.2');
    const rej = within(x).getByTestId('rejected-candidate');
    expect(rej).toHaveTextContent('antigravity / gemini-flash');
    expect(rej).toHaveTextContent('The provider CLI is not installed.');
    expect(rej).toHaveTextContent('The model tier is below what this task requires.');
    expect(within(x).getByText('task_type:coding.min_tier')).toBeInTheDocument();
    expect(within(x).getByTestId('adaptive')).toHaveTextContent('applied');
    expect(within(x).getAllByText(/25 verified runs, 92% success/).length).toBeGreaterThan(0);
    expect(within(x).getByTestId('fallback')).toHaveTextContent('switch');
    expect(within(x).getByTestId('fallback')).toHaveTextContent('claude is rate limited');
    expect(within(row).getByRole('button', { expanded: true })).toBeInTheDocument();
  });

  it('filters by the task in the address (?task=) and by the picker', async () => {
    m.routingDecisions.mockResolvedValue([decision]);
    renderView('/routing?task=tsk_1');
    await screen.findByTestId('decision');
    expect(m.routingDecisions).toHaveBeenCalledWith({ task_id: 'tsk_1', limit: 100 });
    await userEvent.selectOptions(screen.getByLabelText('Filter decisions by task'), '');
    await waitFor(() => expect(m.routingDecisions).toHaveBeenLastCalledWith({ task_id: undefined, limit: 100 }));
  });
});

describe('routing preview', () => {
  const noCandidate = (outcome: RoutingPreview['outcome'], extra: Partial<RoutingPreview['decision']>): RoutingPreview => ({
    outcome,
    classification: cls,
    decision: {
      provider: null,
      model: null,
      candidates: [],
      rejected: [{ provider: 'claude', model: 'claude-sonnet', filter: 'usage_exhausted', filters: ['usage_exhausted'], detail: 'usage state EXHAUSTED: weekly limit' }],
      reasons: [],
      rule_ids: [],
      ...extra,
    },
  });

  async function submit(title = 'Add a settings page') {
    await userEvent.type(await screen.findByLabelText('Title'), title);
    await userEvent.click(screen.getByRole('button', { name: /Preview/ }));
  }

  it('sends the form and renders the explanation without side effects', async () => {
    m.routingPreview.mockResolvedValue({
      outcome: 'route',
      classification: cls,
      decision: { provider: 'claude', model: 'claude-sonnet', candidates: decision.candidates, rejected: [], reasons: ['best fit'], rule_ids: ['r1'], adaptive_applied: false },
    });
    renderView();
    await submit();
    expect(m.routingPreview).toHaveBeenCalledWith({ title: 'Add a settings page', description: '', task_type: null, role: null, phase: 'execute' });
    const region = await screen.findByRole('region', { name: 'Preview result' });
    expect(within(region).getByTestId('outcome')).toHaveAttribute('data-outcome', 'route');
    expect(within(region).getByText(/Chosen:/)).toHaveTextContent('claude / claude-sonnet');
    expect(within(region).getAllByTestId('candidate')).toHaveLength(2);
  });

  it('wait_for_quota: explains the reason and shows the reset', async () => {
    m.routingPreview.mockResolvedValue(noCandidate('wait_for_quota', { wait_for_quota: true, resets_at: '2099-01-01T00:00:00Z', no_candidate_reason: 'every suitable model is out of quota until its reset' }));
    renderView();
    await submit();
    const region = await screen.findByRole('region', { name: 'Preview result' });
    expect(within(region).getByTestId('outcome')).toHaveAttribute('data-outcome', 'wait_for_quota');
    expect(within(region).getByTestId('no-candidate')).toHaveTextContent('every suitable model is out of quota until its reset');
    expect(within(region).getByTestId('reset-countdown')).toBeInTheDocument();
    expect(within(region).getByTestId('rejected-candidate')).toHaveTextContent('The provider quota is exhausted.');
  });

  it('needs_approval and blocked outcomes carry their reason', async () => {
    m.routingPreview.mockResolvedValueOnce(noCandidate('needs_approval', { requires_approval: true, no_candidate_reason: 'risk critical: explicit approval required before executing' }));
    renderView();
    await submit('Drop the production database');
    let region = await screen.findByRole('region', { name: 'Preview result' });
    expect(within(region).getByTestId('outcome')).toHaveAttribute('data-outcome', 'needs_approval');
    expect(within(region).getByTestId('no-candidate')).toHaveTextContent('explicit approval required');

    m.routingPreview.mockResolvedValueOnce(noCandidate('blocked', { no_candidate_reason: 'no installed provider has the capability image_generation' }));
    await userEvent.click(screen.getByRole('button', { name: /Preview/ }));
    await waitFor(() => expect(screen.getByRole('region', { name: 'Preview result' }).querySelector('[data-outcome="blocked"]')).not.toBeNull());
    region = screen.getByRole('region', { name: 'Preview result' });
    expect(within(region).getByTestId('no-candidate')).toHaveTextContent('no installed provider has the capability image_generation');
  });

  it('does not submit an empty title', async () => {
    renderView();
    await screen.findByLabelText('Title');
    expect(screen.getByRole('button', { name: /Preview/ })).toBeDisabled();
  });
});
