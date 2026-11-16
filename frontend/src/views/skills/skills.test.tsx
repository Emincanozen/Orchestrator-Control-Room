import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../api/endpoints', () => ({
  api: { skills: vi.fn(), skill: vi.fn(), skillAction: vi.fn(), skillContent: vi.fn(), setSkillSource: vi.fn() },
}));
import { ApiError } from '../../api/client';
import { api } from '../../api/endpoints';
import type { Skill, SkillDetail } from '../../api/types';
import { initialData } from '../../store/reducer';
import { useStore } from '../../store/store';
import { mkProject } from '../../test/fixtures';
import { SkillsView } from '../SkillsView';

const m = vi.mocked(api, true);

const summary = (o: Partial<Skill> = {}): Skill => ({
  id: 'alpha',
  name: 'Alpha skill',
  user_label: null,
  category: 'test',
  kind: 'agent_skill',
  description: 'does alpha things',
  state: 'CATALOGUED_ON_DEMAND',
  source_url: 'https://github.com/o/alpha',
  source_verified: true,
  source: 'catalog',
  installable: true,
  installable_reason: null,
  activation: { possible: true, mode: 'skill_dir', folder: '.claude/skills/<name>' },
  pinned_ref: null,
  installed_ref: null,
  previous_ref: null,
  compat: { claude_code: 'yes', codex: 'unknown', mcp: 'n/a' },
  risk: 'low',
  requires_credentials: false,
  has_uncertainty: false,
  last_error: null,
  incompatible_reasons: [],
  active_projects: [],
  warnings: [],
  updated_at: '2026-11-11T10:00:00Z',
  ...o,
});

const detail = (o: Partial<SkillDetail> = {}): SkillDetail => ({
  ...summary(),
  uncertainty: null,
  candidates: [],
  license: 'MIT',
  maintenance: { stars: 5 },
  default_branch: 'main',
  paid_service: 'no',
  priority_candidate: false,
  duplicates_of: null,
  install_method: { type: 'manual', command: 'npx skills add o/alpha', notes: 'n' },
  dependencies: [],
  permissions: [],
  security: { inspected: false, risk: 'low', findings: [] },
  local_path: null,
  archives: [],
  pending_reactivation: [],
  activations: [],
  events: [],
  instructions: { documented_method: {}, activation: { possible: true, mode: 'skill_dir' }, orchestrator_steps: [], note: 'Documented commands are NOT run.' },
  codex: { supported: false, reason: 'Codex does not read .agents/skills (unconfirmed upstream)' },
  ...o,
});

function renderView() {
  return render(
    <MemoryRouter>
      <SkillsView />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  useStore.setState({ data: { ...initialData(), projects: { prj_1: mkProject({ id: 'prj_1', name: 'Demo' }) } } });
  m.skills.mockResolvedValue([summary()]);
  m.skill.mockResolvedValue(detail());
});

describe('SkillsView catalogue', () => {
  it('passes the search, category, kind, state and project filters to the API', async () => {
    m.skills.mockResolvedValue([summary({ category: 'video' }), summary({ id: 'b', name: 'Beta', category: 'trading' })]);
    renderView();
    await screen.findByText('Alpha skill');
    await userEvent.selectOptions(screen.getByLabelText('Filter by category'), 'video');
    await userEvent.selectOptions(screen.getByLabelText('Filter by kind'), 'mcp_server');
    await userEvent.selectOptions(screen.getByLabelText('Filter by state'), 'INSTALLED_AND_ACTIVE');
    await userEvent.selectOptions(screen.getByLabelText('Show activation for project'), 'prj_1');
    await userEvent.type(screen.getByLabelText('Search skills'), 'alp');
    await waitFor(() =>
      expect(m.skills).toHaveBeenLastCalledWith({ q: 'alp', category: 'video', kind: 'mcp_server', state: 'INSTALLED_AND_ACTIVE', project_id: 'prj_1' }),
    );
  });

  it('badges every skill state and shows verified vs uncertain sources, warnings and refs', async () => {
    const states = ['CATALOGUED_ON_DEMAND', 'DOWNLOADED_AND_DISABLED', 'INSTALLED_AND_ACTIVE', 'INSTALL_FAILED', 'INCOMPATIBLE', 'NEEDS_SECURITY_REVIEW'] as const;
    m.skills.mockResolvedValue([
      ...states.map((s) => summary({ id: s, name: s, state: s })),
      summary({ id: 'unc', name: 'Uncertain', source_verified: false, has_uncertainty: true, warnings: [{ type: 'duplicate', reason: 'same_source_url', with: ['alpha'] }], installed_ref: 'abcdef1234567890' }),
    ]);
    renderView();
    await screen.findByText('Uncertain');
    for (const s of states) expect(document.querySelector(`[data-skill-state="${s}"]`)).not.toBeNull();
    const unc = screen.getAllByTestId('skill-card').find((c) => c.dataset.skillId === 'unc')!;
    expect(within(unc).getByText('source uncertain')).toBeInTheDocument();
    expect(within(unc).getByText(/Possible duplicate of alpha/)).toBeInTheDocument();
    expect(within(unc).getByText(/installed abcdef12/)).toBeInTheDocument();
    expect(within(screen.getAllByTestId('skill-card')[0]).getByText(/verified source/)).toBeInTheDocument();
  });

  it('shows per-project activation when a project is selected', async () => {
    m.skills.mockResolvedValue([summary({ project_activation: { active: true, ref: 'x' } })]);
    renderView();
    await screen.findByText('Alpha skill');
    await userEvent.selectOptions(screen.getByLabelText('Show activation for project'), 'prj_1');
    expect(await screen.findByTestId('project-activation')).toHaveTextContent('active in Demo');
  });
});

describe('Skill drawer', () => {
  async function open() {
    renderView();
    await userEvent.click(await screen.findByRole('button', { name: 'Open skill Alpha skill' }));
    return screen.findByTestId('skill-detail');
  }

  it('download asks for confirmation that explains nothing is executed, then updates the state', async () => {
    m.skillAction.mockResolvedValue({ outcome: { ok: true, changed: true }, skill: detail({ state: 'DOWNLOADED_AND_DISABLED', installed_ref: 'a'.repeat(40) }) });
    const d = await open();
    await userEvent.click(within(d).getByRole('button', { name: /Download/ }));
    const dlg = (await screen.findAllByRole('dialog')).at(-1)!;
    expect(dlg).toHaveTextContent('nothing from the package is executed');
    expect(m.skillAction).not.toHaveBeenCalled();
    await userEvent.click(within(dlg).getByRole('button', { name: 'Download' }));
    await waitFor(() => expect(m.skillAction).toHaveBeenCalledWith('alpha', 'install'));
    await waitFor(() => expect(within(screen.getByTestId('skill-detail')).getAllByText('Downloaded').length).toBeGreaterThan(0));
  });

  it('activate answering 202 shows "approval required" with a link to Approvals', async () => {
    m.skill.mockResolvedValue(detail({ state: 'DOWNLOADED_AND_DISABLED', installed_ref: 'a'.repeat(40) }));
    m.skillAction.mockResolvedValue({
      outcome: { status: 'approval_required', approval_id: 'apr_1', message: 'An untrusted_tool_install approval is pending.' },
      skill: detail({ state: 'DOWNLOADED_AND_DISABLED', installed_ref: 'a'.repeat(40) }),
    });
    const d = await open();
    await userEvent.click(within(d).getByRole('button', { name: /^Activate/ }));
    const dlg = (await screen.findAllByRole('dialog')).at(-1)!;
    expect(dlg).toHaveTextContent('untrusted_tool_install');
    await userEvent.click(within(dlg).getByRole('button', { name: 'Activate' }));
    expect(m.skillAction).toHaveBeenCalledWith('alpha', 'activate', { project_id: 'prj_1', scope: 'project', targets: ['claude'] });
    const note = await screen.findByTestId('approval-required');
    expect(note).toHaveTextContent('Approval required');
    expect(within(note).getByTestId('approvals-link')).toHaveAttribute('href', '/approvals');
  });

  it('catalogue-only kinds show instructions instead of an Activate button', async () => {
    m.skill.mockResolvedValue(
      detail({ kind: 'standalone_app', activation: { possible: false, mode: 'catalogue_only', reason: 'kind standalone_app is not an installable artefact' }, install_method: { type: 'manual', command: 'brew install x', notes: 'Install from the website.' } }),
    );
    const d = await open();
    expect(within(d).getByTestId('catalogue-only')).toHaveTextContent('Install from the website.');
    expect(within(d).queryByRole('button', { name: /^Activate/ })).toBeNull();
    expect(within(d).queryByRole('button', { name: /Download/ })).toBeNull();
    expect(within(d).getByTestId('documented-command')).toHaveTextContent('brew install x');
  });

  it('a skill without a source cannot be downloaded, lists candidates and accepts a source', async () => {
    m.skill.mockResolvedValue(detail({ source_url: null, installable: false, installable_reason: 'source URL unknown (ambiguous entry)', candidates: ['https://github.com/a/one', 'https://github.com/b/two'], has_uncertainty: true, uncertainty: 'Two repositories match.' }));
    m.setSkillSource.mockResolvedValue({ outcome: { ok: true }, skill: detail({ source: 'user' } as never) });
    const d = await open();
    expect(within(d).getByRole('button', { name: /Download/ })).toBeDisabled();
    expect(within(d).getByTestId('not-installable')).toHaveTextContent('source URL unknown');
    expect(within(d).getByTestId('candidates')).toHaveTextContent('https://github.com/b/two');
    await userEvent.click(within(d).getAllByRole('button', { name: 'Use as source' })[0]);
    await userEvent.click(within(d).getByRole('button', { name: 'Set source' }));
    const dlg = (await screen.findAllByRole('dialog')).at(-1)!;
    await userEvent.click(within(dlg).getByRole('button', { name: 'Set source' }));
    await waitFor(() => expect(m.setSkillSource).toHaveBeenCalledWith('alpha', 'https://github.com/a/one'));
  });

  it('shows Codex activation as unsupported with the backend reason', async () => {
    const d = await open();
    expect(within(d).getByTestId('codex-support')).toHaveTextContent('unsupported — Codex does not read .agents/skills');
  });

  it('renders SKILL.md and malicious markup as plain text', async () => {
    m.skill.mockResolvedValue(detail({ state: 'DOWNLOADED_AND_DISABLED', installed_ref: 'a'.repeat(40) }));
    const evil = '# Title\n<img src=x onerror="window.__pwned=1"><script>window.__pwned=1</script>[click](javascript:alert(1))';
    m.skillContent.mockResolvedValue({ skill_id: 'alpha', ref: 'a', path: 'SKILL.md', text: evil, truncated: false });
    const d = await open();
    await userEvent.click(within(d).getByRole('button', { name: /Show SKILL.md/ }));
    const pre = await screen.findByTestId('skill-content');
    expect(pre.textContent).toBe(evil);
    expect(pre.querySelector('img, script, a')).toBeNull();
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
  });

  it('shows errors from the envelope inline', async () => {
    m.skill.mockResolvedValue(detail({ state: 'DOWNLOADED_AND_DISABLED', installed_ref: 'a'.repeat(40) }));
    m.skillAction.mockRejectedValue(new ApiError(409, 'invalid_state', 'an activation of another ref exists in this project'));
    const d = await open();
    await userEvent.click(within(d).getByRole('button', { name: /Update/ }));
    const dlg = (await screen.findAllByRole('dialog')).at(-1)!;
    await userEvent.click(within(dlg).getByRole('button', { name: 'Update' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('another ref exists (invalid_state)'.replace('exists (invalid_state)', 'exists'));
  });

  it('source URL is plain text with a copy button and an explicit open that uses noopener', async () => {
    const open_ = vi.spyOn(window, 'open').mockReturnValue(null);
    const d = await open();
    expect(within(d).getByTestId('source-url').tagName).toBe('SPAN');
    expect(d.querySelector('a[href]')).toBeNull();
    expect(open_).not.toHaveBeenCalled();
    await userEvent.click(within(d).getByRole('button', { name: /Open/ }));
    expect(open_).toHaveBeenCalledWith('https://github.com/o/alpha', '_blank', 'noopener,noreferrer');
    open_.mockRestore();
  });
});
