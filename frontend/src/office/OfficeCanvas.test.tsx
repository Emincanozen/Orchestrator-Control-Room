import { StrictMode } from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { mkAgent } from '../test/fixtures';

const created: { mount: ReturnType<typeof vi.fn>; destroy: ReturnType<typeof vi.fn> }[] = [];
vi.mock('./engine/pixiOffice', () => ({
  createPixiOffice: () => {
    const e = {
      mount: vi.fn(async () => {}),
      destroy: vi.fn(),
      setAgents: vi.fn(),
      setSelected: vi.fn(),
      setReducedMotion: vi.fn(),
      zoomIn: vi.fn(),
      zoomOut: vi.fn(),
      fit: vi.fn(),
    };
    created.push(e);
    return e;
  },
}));

import { OfficeCanvas } from './OfficeCanvas';

describe('OfficeCanvas', () => {
  it('mounts and unmounts cleanly under StrictMode (no leaked engines)', () => {
    created.length = 0;
    const { unmount } = render(
      <StrictMode>
        <OfficeCanvas agents={[mkAgent()]} onSelectAgent={() => {}} />
      </StrictMode>,
    );
    expect(created.length).toBe(2);
    expect(created[0].destroy).toHaveBeenCalledTimes(1);
    expect(created[1].destroy).not.toHaveBeenCalled();
    expect(created[1].mount).toHaveBeenCalledTimes(1);
    unmount();
    expect(created[1].destroy).toHaveBeenCalledTimes(1);
  });

  it('selects an agent from the accessible button list', async () => {
    const onSelect = vi.fn();
    render(<OfficeCanvas agents={[mkAgent({ id: 'agt_1', name: 'Ada' }), mkAgent({ id: 'agt_2', name: 'Bob' })]} onSelectAgent={onSelect} />);
    await userEvent.click(screen.getByRole('button', { name: /Bob/ }));
    expect(onSelect).toHaveBeenCalledWith('agt_2');
  });

  it('shows an honest empty state without agents', () => {
    render(<OfficeCanvas agents={[]} onSelectAgent={() => {}} />);
    expect(screen.getByText(/No agents recorded/)).toBeInTheDocument();
  });
});
