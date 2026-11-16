/** Plain-language text for backend vocabulary. Unknown keys fall back to the raw value: never invented prose. */
import type { UsageState } from '../api/types';

export const BILLING_TEXT: Record<string, { label: string; text: string }> = {
  subscription_cli: {
    label: 'subscription CLI',
    text: 'Runs through a logged-in CLI on your plan. Usage counts against the plan quota, not a per-token bill.',
  },
  metered_api: {
    label: 'metered API',
    text: 'Billed per token through an API key. The router never uses it unless policies allow this billing class.',
  },
  local: { label: 'local', text: 'Runs on this machine. No quota and no bill.' },
  unknown: { label: 'unknown billing', text: 'The billing class is not configured, so the router treats it as not allowed.' },
};

export function billingInfo(cls: string | null | undefined): { label: string; text: string } {
  if (!cls) return BILLING_TEXT.unknown;
  return BILLING_TEXT[cls] ?? { label: cls, text: 'Unrecognised billing class; shown as reported by the backend.' };
}

export const USAGE_STATE_TEXT: Record<UsageState, { label: string; color: string; text: string }> = {
  AVAILABLE: { label: 'Available', color: 'var(--color-ok)', text: 'Quota is available.' },
  LOW: { label: 'Low', color: 'var(--color-amber)', text: 'Quota is running low.' },
  EXHAUSTED: { label: 'Exhausted', color: 'var(--color-danger)', text: 'Quota is used up until the reset time.' },
  COOLDOWN: { label: 'Cooldown', color: 'var(--color-st-waiting-for-quota)', text: 'A rate limit is active; wait before retrying.' },
  UNKNOWN: { label: 'Unknown', color: 'var(--color-faint)', text: 'The provider does not expose this.' },
  AUTH_ERROR: { label: 'Auth error', color: 'var(--color-danger)', text: 'The CLI is not logged in or its credentials were rejected.' },
  PROVIDER_ERROR: { label: 'Provider error', color: 'var(--color-danger)', text: 'The provider reported an error.' },
};

export const CAPABILITY_FLAGS: { key: string; label: string; hint: string }[] = [
  { key: 'structured_stream', label: 'Structured output stream', hint: 'Emits machine-readable events while it works.' },
  { key: 'resume', label: 'Session resume', hint: 'A failed run can continue the same session.' },
  { key: 'cancel', label: 'Cancel', hint: 'A run can be stopped on request.' },
  { key: 'model_selection', label: 'Model selection', hint: 'The caller can choose the model.' },
  { key: 'model_discovery', label: 'Model discovery', hint: 'The CLI can list its models.' },
  { key: 'usage_tokens', label: 'Token usage', hint: 'Reports token counts per run.' },
  { key: 'usage_cost', label: 'Cost reporting', hint: 'Reports a monetary cost.' },
  { key: 'rate_limit_info', label: 'Rate-limit info', hint: 'Reports quota windows or reset times.' },
  { key: 'json_schema_output', label: 'JSON-schema output', hint: 'Can constrain output to a schema.' },
];

export const EXACTNESS_TEXT: Record<string, string> = {
  exact: 'Reported by the provider as an exact figure.',
  estimated: 'An estimate, not a provider-reported figure.',
  unknown: 'The provider does not expose this value.',
};

/** Why the router dropped a candidate, keyed by the backend filter name (router/router.py). */
export const FILTER_TEXT: Record<string, string> = {
  not_installed: 'The provider CLI is not installed.',
  not_authenticated: 'The provider reports that it is not logged in.',
  missing_capability: 'The model lacks a capability this task needs.',
  usage_exhausted: 'The provider quota is exhausted.',
  usage_cooldown: 'The provider is in a rate-limit cooldown.',
  usage_auth_error: 'The provider has an authentication error.',
  usage_provider_error: 'The provider is reporting errors.',
  billing_class: 'Its billing class is not allowed by policy.',
  disabled: 'Disabled by a routing preference.',
  excluded: 'Excluded for this task (for example after a failure).',
  below_min_tier: 'The model tier is below what this task requires.',
};

export function filterText(filter: string): string {
  return FILTER_TEXT[filter] ?? filter;
}

export const COMPONENT_LABEL: Record<string, string> = {
  tier_fit: 'Tier fit',
  user_preference: 'Preference',
  context_continuity: 'Continuity',
  usage_headroom: 'Headroom',
  historical_success: 'History',
};

export const OUTCOME_TEXT: Record<string, { label: string; color: string; text: string }> = {
  route: { label: 'Routed', color: 'var(--color-ok)', text: 'A model was selected.' },
  wait_for_quota: { label: 'Wait for quota', color: 'var(--color-st-waiting-for-quota)', text: 'Every otherwise suitable model is out of quota; the task would wait for a reset.' },
  needs_approval: { label: 'Needs approval', color: 'var(--color-st-needs-approval)', text: 'The task is classified as critical risk; it would wait for your approval.' },
  blocked: { label: 'Blocked', color: 'var(--color-danger)', text: 'No model can run this task right now.' },
};

/** Countdown text for a future timestamp ("2h 05m 10s"); null when no/invalid/past target. */
export function countdown(targetMs: number | null, nowMs: number): string | null {
  if (targetMs === null) return null;
  const s = Math.floor((targetMs - nowMs) / 1000);
  if (s <= 0) return 'reset time reached';
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const p = (n: number) => String(n).padStart(2, '0');
  if (d > 0) return `${d}d ${p(h)}h ${p(m)}m`;
  if (h > 0) return `${h}h ${p(m)}m ${p(sec)}s`;
  return `${m}m ${p(sec)}s`;
}
