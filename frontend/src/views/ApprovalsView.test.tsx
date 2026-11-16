import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import type { Approval } from '../api/types';
import { initialData } from '../store/reducer';
import { useStore } from '../store/store';
import { mkProject } from '../test/fixtures';
import { ApprovalCard } from './ApprovalsView';

const mk = (kind: string, details: Record<string, unknown>, summary = 'Backend summary of what approving grants'): Approval => ({
  id: 'apr_1', task_id: null, kind, summary, details, status: 'pending', requested_at: '2026-11-11T10:00:00Z', resolved_at: null, resolved_by: null, note: null,
});

beforeEach(() => useStore.setState({ data: { ...initialData(), projects: { prj_1: mkProject({ id: 'prj_1', name: 'Demo' }) } } }));

describe('ApprovalCard details', () => {
  it('renders an untrusted_tool_install readably', () => {
    render(<ul><ApprovalCard approval={mk('untrusted_tool_install', { skill_name: 'Alpha', ref: 'abc123', project_id: 'prj_1', source_url: 'https://github.com/o/a', risk: 'medium', review_reasons: ['contains hooks'], findings: ['high: hook (hooks/x.sh)'], mode: 'skill_dir' })} /></ul>);
    const f = screen.getByTestId('approval-facts');
    expect(f).toHaveTextContent('Alpha');
    expect(f).toHaveTextContent('abc123');
    expect(f).toHaveTextContent('Demo');
    expect(f).toHaveTextContent('contains hooks');
    expect(screen.getByText('Backend summary of what approving grants')).toBeInTheDocument();
  });
  it('renders permission_escalation and high_risk_task', () => {
    const { unmount } = render(<ul><ApprovalCard approval={mk('permission_escalation', { phase: 'plan', provider: 'claude', permission: 'read_only', grants: 'workspace_write' })} /></ul>);
    expect(screen.getByTestId('approval-facts')).toHaveTextContent('workspace_write');
    unmount();
    render(<ul><ApprovalCard approval={mk('high_risk_task', { decision: { task_type: 'ops', would_route_to: 'claude/x', reasons: ['critical risk'] } })} /></ul>);
    expect(screen.getByTestId('approval-facts')).toHaveTextContent('claude/x');
    expect(screen.getByTestId('approval-facts')).toHaveTextContent('critical risk');
  });
});
