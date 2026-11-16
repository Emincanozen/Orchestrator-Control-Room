import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api/endpoints', () => ({ api: { getSettings: vi.fn(), putSettings: vi.fn() } }));
import { api } from '../api/endpoints';
import type { SettingsPayload } from '../api/types';
import { SettingsView, validateLimit } from './SettingsView';

const m = vi.mocked(api, true);
const payload = (limits: Record<string, number>, overrides: Record<string, number> = {}): SettingsPayload => ({
  defaults: { limits: { max_parallel_workers: 3, max_retry_attempts: 3 } },
  overrides: Object.keys(overrides).length ? { limits: overrides } : {},
  effective: { limits },
});

beforeEach(() => vi.clearAllMocks());

describe('validateLimit', () => {
  it('mirrors the backend rules', () => {
    expect(validateLimit('max_parallel_workers', '2')).toBeNull();
    expect(validateLimit('max_parallel_workers', '0')).toMatch(/greater than zero/);
    expect(validateLimit('max_retry_attempts', '0')).toBeNull();
    expect(validateLimit('max_parallel_workers', '-1')).toMatch(/negative/);
    expect(validateLimit('max_parallel_workers', 'abc')).toMatch(/number/);
    expect(validateLimit('max_parallel_workers', '1.5')).toMatch(/whole/);
    expect(validateLimit('max_parallel_workers', '')).toBe('Required');
  });
});

describe('SettingsView', () => {
  it('saves only the changed limits and blocks invalid input', async () => {
    m.getSettings.mockResolvedValue(payload({ max_parallel_workers: 3, max_retry_attempts: 3 }));
    m.putSettings.mockResolvedValue(payload({ max_parallel_workers: 2, max_retry_attempts: 3 }, { max_parallel_workers: 2 }));
    render(<SettingsView />);
    const workers = await screen.findByLabelText(/Max parallel workers/);
    const save = screen.getByRole('button', { name: /Save limits/ });
    expect(save).toBeDisabled(); // nothing changed
    await userEvent.clear(workers);
    await userEvent.type(workers, '0');
    expect(screen.getByText('Must be greater than zero')).toBeInTheDocument();
    expect(save).toBeDisabled();
    await userEvent.clear(workers);
    await userEvent.type(workers, '2');
    await userEvent.click(save);
    expect(m.putSettings).toHaveBeenCalledWith({ limits: { max_parallel_workers: 2 } });
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved'));
    expect(screen.getByText('overridden')).toBeInTheDocument();
  });
});
