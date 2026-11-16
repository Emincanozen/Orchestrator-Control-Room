import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../api/endpoints', () => ({
  api: { providers: vi.fn(), models: vi.fn(), usage: vi.fn(), refreshProviders: vi.fn() },
}));
import { api } from '../../api/endpoints';
import type { ProviderModel, ProviderStatus, UsageEntry } from '../../api/types';
import { ProvidersView } from '../ProvidersView';
import { ResetCountdown } from './parts';

const m = vi.mocked(api, true);

const caps = {
  structured_stream: true,
  resume: false,
  cancel: true,
  model_selection: true,
  model_discovery: false,
  usage_tokens: true,
  usage_cost: false,
  rate_limit_info: false,
  json_schema_output: false,
  sandbox_modes: ['read_only', 'workspace_write'],
  task_capabilities: ['coding'],
};

const provider = (o: Partial<ProviderStatus> = {}): ProviderStatus => ({
  provider: 'claude',
  detected: true,
  installed: true,
  version: '2.1.0',
  executable: 'C:\\bin\\claude.exe',
  authenticated: true,
  auth_detail: 'logged in via claude.ai',
  error: null,
  capabilities: caps,
  billing_class: 'subscription_cli',
  checked_at: '2026-11-11T10:00:00Z',
  model_count: 2,
  usage: { state: 'UNKNOWN', reason: null, resets_at: null, until: null, confidence: null, evidence_metric: null },
  ...o,
});

const model = (o: Partial<ProviderModel> = {}): ProviderModel => ({
  provider: 'claude',
  model_id: 'claude-sonnet',
  alias: 'sonnet',
  display_name: null,
  tier: 'standard',
  tier_source: 'config',
  capabilities: ['coding'],
  listed_capabilities: ['coding', 'image_understanding'],
  billing_class: 'subscription_cli',
  verification: 'listed',
  source: 'config',
  discovered: false,
  provider_installed: true,
  ...o,
});

function usageEntry(o: Partial<UsageEntry> = {}): UsageEntry {
  return {
    provider: 'claude',
    model: null,
    usage_state: { state: 'UNKNOWN', reason: null, resets_at: null, until: null, confidence: null, evidence_metric: null },
    values: [],
    ...o,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  m.providers.mockResolvedValue([provider()]);
  m.models.mockResolvedValue([model()]);
  m.usage.mockResolvedValue([usageEntry()]);
});
afterEach(() => vi.useRealTimers());

describe('ProvidersView', () => {
  it('shows "Cannot be determined" with the detail text when authentication is null', async () => {
    m.providers.mockResolvedValue([provider({ authenticated: null, auth_detail: 'the CLI has no status command' })]);
    render(<ProvidersView />);
    const auth = await screen.findByTestId('auth-claude');
    expect(auth).toHaveTextContent('Cannot be determined');
    expect(auth).toHaveTextContent('the CLI has no status command');
    expect(auth).not.toHaveTextContent(/^Yes/);
  });

  it('renders Yes / No for known authentication', async () => {
    m.providers.mockResolvedValue([provider({ authenticated: true }), provider({ provider: 'codex', authenticated: false, auth_detail: 'not logged in' })]);
    render(<ProvidersView />);
    expect(await screen.findByTestId('auth-claude')).toHaveTextContent('Yes');
    expect(screen.getByTestId('auth-codex')).toHaveTextContent('No');
  });

  it('explains the billing class and states unsupported features explicitly', async () => {
    render(<ProvidersView />);
    const card = await screen.findByTestId('provider-claude');
    expect(within(card).getByText('subscription CLI')).toBeInTheDocument();
    expect(within(card).getByText(/Runs through a logged-in CLI/)).toBeInTheDocument();
    const resume = card.querySelector('[data-cap="resume"]') as HTMLElement;
    expect(resume).toHaveTextContent('not supported');
    expect(card.querySelector('[data-cap="structured_stream"]')).toHaveTextContent('supported');
    expect(card.querySelector('[data-cap="structured_stream"]')).not.toHaveTextContent('not supported');
  });

  it('never renders unknown usage as a number or a percentage', async () => {
    m.usage.mockResolvedValue([
      usageEntry({
        values: [
          { metric: 'tokens_remaining', value: null, unit: 'tokens', exactness: 'unknown', source: 'cli', confidence: null, state: 'UNKNOWN', resets_at: null, observed_at: '2026-11-11T10:00:00Z' },
        ],
      }),
    ]);
    render(<ProvidersView />);
    const entry = await screen.findByTestId('usage-entry');
    expect(within(entry).getByTestId('usage-unknown')).toHaveTextContent('Unknown — the provider does not expose this');
    expect(within(entry).queryByTestId('usage-number')).toBeNull();
    expect(entry.textContent).not.toMatch(/%/);
    expect(entry.querySelector('[role="progressbar"]')).toBeNull();
  });

  it('an unknown-exactness value stays unknown even if a number were present', async () => {
    m.usage.mockResolvedValue([
      usageEntry({
        values: [{ metric: 'x', value: 0, unit: null, exactness: 'unknown', source: 's', confidence: null, state: 'UNKNOWN', resets_at: null, observed_at: '2026-11-11T10:00:00Z' }],
      }),
    ]);
    render(<ProvidersView />);
    const entry = await screen.findByTestId('usage-entry');
    expect(within(entry).queryByTestId('usage-number')).toBeNull();
    expect(within(entry).getByTestId('usage-unknown')).toBeInTheDocument();
  });

  it('shows an exactness chip, source and observed age for every number', async () => {
    m.usage.mockResolvedValue([
      usageEntry({
        usage_state: { state: 'LOW', reason: 'few requests left', resets_at: null, until: null, confidence: 'high', evidence_metric: 'requests' },
        values: [
          { metric: 'input_tokens', value: 1200, unit: 'tokens', exactness: 'exact', source: 'run_result', confidence: 'high', state: 'AVAILABLE', resets_at: null, observed_at: '2026-11-11T10:00:00Z' },
          { metric: 'cost', value: 0.5, unit: 'usd', exactness: 'estimated', source: 'estimate', confidence: 'low', state: 'AVAILABLE', resets_at: null, observed_at: '2026-11-11T10:00:00Z' },
        ],
      }),
    ]);
    render(<ProvidersView />);
    const entry = await screen.findByTestId('usage-entry');
    const rows = within(entry).getAllByRole('row').slice(1);
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText('exact')).toBeInTheDocument();
    expect(within(rows[0]).getByText('run_result')).toBeInTheDocument();
    expect(within(rows[0]).getByTestId('usage-number')).toHaveTextContent('1200 tokens');
    expect(within(rows[1]).getByText('estimated')).toBeInTheDocument();
    expect(within(entry).getByText('few requests left')).toBeInTheDocument();
    expect(entry.querySelector('[data-usage-state="LOW"]')).not.toBeNull();
  });

  it('has a distinct badge for each of the seven usage states', async () => {
    const states = ['AVAILABLE', 'LOW', 'EXHAUSTED', 'COOLDOWN', 'UNKNOWN', 'AUTH_ERROR', 'PROVIDER_ERROR'] as const;
    m.providers.mockResolvedValue(
      states.map((s) => provider({ provider: `p_${s}`, usage: { state: s, reason: null, resets_at: null, until: null, confidence: null, evidence_metric: null } })),
    );
    render(<ProvidersView />);
    await screen.findByTestId('provider-p_AVAILABLE');
    for (const s of states) {
      const card = screen.getByTestId(`provider-p_${s}`);
      expect(card.querySelector(`[data-usage-state="${s}"]`)).not.toBeNull();
    }
  });

  it('flags listed-only capabilities as unverified and filters the model table', async () => {
    m.models.mockResolvedValue([
      model(),
      model({ provider: 'codex', model_id: 'gpt-x', alias: null, tier: 'advanced', tier_source: 'default', capabilities: ['coding'], listed_capabilities: [], verification: 'verified', discovered: true }),
    ]);
    render(<ProvidersView />);
    const table = await screen.findByRole('table', { name: 'Models' });
    expect(within(table).getByText('image_understanding (listed, unverified)')).toBeInTheDocument();
    expect(within(table).getByText('default (not configured)')).toBeInTheDocument();
    expect(within(table).getAllByTestId('model-row')).toHaveLength(2);
    await userEvent.selectOptions(screen.getByLabelText('Filter by provider'), 'codex');
    expect(within(table).getAllByTestId('model-row')).toHaveLength(1);
    expect(within(table).getByText('gpt-x')).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText('Filter by provider'), '');
    await userEvent.selectOptions(screen.getByLabelText('Filter by capability'), 'image_understanding');
    expect(within(table).getAllByTestId('model-row')).toHaveLength(1);
  });

  it('Refresh posts and reloads the lists', async () => {
    m.refreshProviders.mockResolvedValue([]);
    render(<ProvidersView />);
    await screen.findByTestId('provider-claude');
    await userEvent.click(screen.getByRole('button', { name: /Refresh/ }));
    await waitFor(() => expect(m.refreshProviders).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(m.providers.mock.calls.length).toBeGreaterThanOrEqual(2));
  });
});

describe('ResetCountdown', () => {
  it('ticks locally every second', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-11-11T10:00:00Z'));
    render(<ResetCountdown iso="2026-11-11T11:01:01Z" />);
    expect(screen.getByTestId('reset-countdown')).toHaveTextContent('resets in 1h 01m 01s');
    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(screen.getByTestId('reset-countdown')).toHaveTextContent('resets in 1h 00m 56s');
  });

  it('says the reset time was reached instead of going negative', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-11-11T12:00:00Z'));
    render(<ResetCountdown iso="2026-11-11T11:00:00Z" />);
    expect(screen.getByTestId('reset-countdown')).toHaveTextContent('reset time reached');
  });
});
