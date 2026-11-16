import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it } from 'vitest';
import type { Approval, ProviderAvailability } from '../api/types';
import { VerifyChecks, VerifyOriginChip } from '../components/VerifyChecks';
import { Shell } from '../components/Shell';
import { initialData } from '../store/reducer';
import { useStore } from '../store/store';
import { ApprovalCard } from './ApprovalsView';
import { AvailabilityList, PromptSizes } from './analytics/Availability';

const mk = (kind: string, details: Record<string, unknown>, summary = 'Backend summary'): Approval => ({
  id: 'apr_1', task_id: null, kind, summary, details, status: 'pending', requested_at: '2026-11-11T10:00:00Z', resolved_at: null, resolved_by: null, note: null,
});
const blank = { name: null, cmd: null, timeout: null, file_exists: null, contains: null, file_absent: null, file_unchanged: null, json_valid: null };

beforeEach(() => useStore.setState({ data: initialData(), conn: 'idle' }));

describe('leader state', () => {
  it('shows detecting instead of a missing leader', () => {
    useStore.setState({ data: { ...initialData(), leaderState: 'detecting' } });
    render(<MemoryRouter><Shell /></MemoryRouter>);
    expect(screen.getByTestId('leader')).toHaveTextContent('detecting');
  });
});

describe('analytics shapes', () => {
  it('shows a null share as unknown with the observation counts and exactness', () => {
    const v: Record<string, ProviderAvailability> = {
      codex: { available_share: null, known_observations: 0, unknown_observations: 4, observations: 4, last_observed_at: null, exactness: 'missing' },
      claude: { available_share: 0.75, known_observations: 8, unknown_observations: 2, observations: 10, last_observed_at: 'x', exactness: 'estimated' },
    };
    render(<AvailabilityList value={v} />);
    const codex = screen.getByTestId('availability-codex');
    expect(within(codex).getByTestId('availability-share')).toHaveTextContent('unknown');
    expect(codex).toHaveTextContent('0 known, 4 unknown of 4 obs');
    expect(codex).toHaveTextContent('missing');
    expect(screen.getByTestId('availability-claude')).toHaveTextContent('75%');
  });
  it('renders handoff prompt size distributions incl. max', () => {
    render(<PromptSizes value={{ prompt_chars: { p50: 10, p90: 20, mean: 12, max: 30 }, handoff_chars: null, knowledge_chars: null } as never} />);
    expect(screen.getByTestId('prompt-size-prompt')).toHaveTextContent('30');
    expect(screen.getByTestId('prompt-size-handoff')).toHaveTextContent('not recorded');
  });
});

describe('verify rendering', () => {
  it('lists structured checks and the origin', () => {
    render(<><VerifyChecks checks={[{ ...blank, json_valid: 'a.json' }, { ...blank, file_exists: 'b', contains: 'c' }]} /><VerifyOriginChip origin="approved" /></>);
    expect(screen.getAllByRole('listitem')).toHaveLength(2);
    expect(screen.getByText('valid JSON: a.json')).toBeInTheDocument();
    expect(screen.getByTestId('verify-origin')).toHaveTextContent('approved');
  });
});

describe('approvals', () => {
  it('renders structured checks of a leader_verify_commands approval', () => {
    render(<ul><ApprovalCard approval={mk('leader_verify_commands', { source: 'objective', verify_commands: [{ ...blank, file_absent: 'x.tmp' }], unsafe: [{ command: 'curl x', reason: 'network' }] })} /></ul>);
    expect(screen.getByTestId('approval-verify-checks')).toHaveTextContent('must not exist: x.tmp');
    expect(screen.getByText('curl x')).toBeInTheDocument();
  });
  it('shows the exact MCP command, env key names and short hashes on a skill activation', () => {
    const sha = 'a'.repeat(64);
    render(<ul><ApprovalCard approval={mk('untrusted_tool_install', { skill_name: 'S', mode: 'mcp_server', content_sha256: sha, mcp_entry_sha256: 'b'.repeat(64), activation: { file: '.mcp.json', server: 'srv', command: 'npx', args: ['-y', 'pkg'], url: null, transport: 'stdio', env_keys: ['API_KEY'] } })} /></ul>);
    expect(screen.getByTestId('activation-command')).toHaveTextContent('npx -y pkg');
    expect(screen.getByTestId('activation-env')).toHaveTextContent('API_KEY');
    expect(screen.getByTestId('activation-content-hash')).toHaveTextContent('a'.repeat(16));
    expect(screen.getByTestId('activation-content-hash').textContent).not.toContain('a'.repeat(17));
  });
  it('shows the backend permission_escalation summary verbatim and adds no confinement claim', () => {
    const summary = 'For Codex this means its own workspace-write sandbox.';
    const { container } = render(<ul><ApprovalCard approval={mk('permission_escalation', { phase: 'execute', provider: 'codex', permission: 'workspace_write', grants: null }, summary)} /></ul>);
    expect(screen.getByText(summary)).toBeInTheDocument();
    expect(container.textContent?.toLowerCase()).not.toContain('confined');
  });
});
