/**
 * Maps domain enums to the CSS colour tokens defined in tokens.css.
 * Values themselves live only in tokens.css; this module only names them.
 */
import type { AgentActivity, TaskState } from '../api/types';

const v = (name: string) => `var(--color-${name})`;

export const STATE_COLOR: Record<TaskState, string> = {
  CREATED: v('st-created'),
  QUEUED: v('st-queued'),
  PLANNING: v('st-planning'),
  RUNNING: v('st-running'),
  TESTING: v('st-testing'),
  REVIEWING: v('st-reviewing'),
  WAITING_FOR_QUOTA: v('st-waiting-for-quota'),
  NEEDS_APPROVAL: v('st-needs-approval'),
  PAUSED: v('st-paused'),
  BLOCKED: v('st-blocked'),
  FAILED: v('st-failed'),
  COMPLETED: v('st-completed'),
  CANCELLED: v('st-cancelled'),
};

export const ACTIVITY_COLOR: Record<AgentActivity, string> = {
  offline: v('ag-offline'),
  idle: v('ag-idle'),
  planning: v('ag-planning'),
  coding: v('ag-coding'),
  researching: v('ag-researching'),
  testing: v('ag-testing'),
  reviewing: v('ag-reviewing'),
  consulting: v('ag-consulting'),
  rate_limited: v('ag-rate-limited'),
  needs_approval: v('ag-needs-approval'),
  blocked: v('ag-blocked'),
  celebrating: v('ag-celebrating'),
};

export const STATE_LABEL: Record<TaskState, string> = {
  CREATED: 'Created',
  QUEUED: 'Queued',
  PLANNING: 'Planning',
  RUNNING: 'Running',
  TESTING: 'Testing',
  REVIEWING: 'Reviewing',
  WAITING_FOR_QUOTA: 'Waiting for quota',
  NEEDS_APPROVAL: 'Needs approval',
  PAUSED: 'Paused',
  BLOCKED: 'Blocked',
  FAILED: 'Failed',
  COMPLETED: 'Completed',
  CANCELLED: 'Cancelled',
};

export function activityLabel(a: string): string {
  return a.replace(/_/g, ' ');
}

/**
 * Categorical series colours for charts: one fixed colour per ENTITY (provider), never per rank, so a filter
 * that removes a series cannot repaint the others. Dark-surface steps of the dataviz default categorical
 * palette (values live in tokens.css as --color-cat-N).
 */
// Literal names (not built dynamically) so Tailwind keeps the theme variables when it scans this file.
const CAT = [
  'var(--color-cat-1)',
  'var(--color-cat-2)',
  'var(--color-cat-3)',
  'var(--color-cat-4)',
  'var(--color-cat-5)',
  'var(--color-cat-6)',
  'var(--color-cat-7)',
  'var(--color-cat-8)',
];
const NEUTRAL = 'var(--color-cat-neutral)';
const PROVIDER_SLOT: Record<string, number> = { claude: 0, codex: 1, antigravity: 2, mock: 3 };
const FALLBACK_SLOTS = [4, 5, 6, 7];

export function providerColor(provider: string | null | undefined): string {
  if (!provider) return NEUTRAL;
  const known = PROVIDER_SLOT[provider];
  if (known !== undefined) return CAT[known];
  let h = 0;
  for (const ch of provider) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return CAT[FALLBACK_SLOTS[h % FALLBACK_SLOTS.length]];
}
