import type { VerifyCheckSpec, VerifyCommand, VerifyOrigin } from '../api/types';

/**
 * Readable description of a verify check in any of its API shapes. Structured checks (backend VerifyCheck) are
 * objects with exactly one kind set and the unused keys null: file_exists (+contains), file_absent,
 * file_unchanged, json_valid, cmd (argv list or string).
 */
export function verifyToString(v: VerifyCommand): string {
  if (typeof v === 'string') return v;
  if (Array.isArray(v)) return v.join(' ');
  const label = v.name ? `${v.name}: ` : '';
  if (v.cmd) return label + (Array.isArray(v.cmd) ? v.cmd.join(' ') : v.cmd);
  if (v.file_exists) return `${label}file exists: ${v.file_exists}${v.contains ? ` and contains "${v.contains}"` : ''}`;
  if (v.file_absent) return `${label}file must not exist: ${v.file_absent}`;
  if (v.file_unchanged) return `${label}file unchanged since the base commit: ${v.file_unchanged}`;
  if (v.json_valid) return `${label}valid JSON: ${v.json_valid}`;
  return v.name ?? JSON.stringify(v);
}

/** Short kind label for a structured check ("cmd" for plain strings/argv). */
export function verifyKind(v: VerifyCommand): string {
  if (typeof v === 'string' || Array.isArray(v)) return 'cmd';
  const s = v as VerifyCheckSpec;
  if (s.file_exists) return 'file_exists';
  if (s.file_absent) return 'file_absent';
  if (s.file_unchanged) return 'file_unchanged';
  if (s.json_valid) return 'json_valid';
  return 'cmd';
}

/** Forms edit verify commands as plain strings; strings pass through to the API unchanged. */
export function verifyListToStrings(list: VerifyCommand[] | undefined): string[] {
  return (list ?? []).map(verifyToString);
}

export const VERIFY_ORIGIN_LABEL: Record<VerifyOrigin, string> = {
  user: 'checks written by you (task or project settings)',
  leader: 'checks proposed by the leader and passed the safety filter',
  approved: 'checks proposed by the leader and approved by you',
};

/** Reads `plan.verify_origin` / `plan.verify_not_adopted` from the opaque task plan. */
export function planVerifyInfo(plan: unknown): { origin: VerifyOrigin | null; notAdopted: string | null } {
  if (!plan || typeof plan !== 'object') return { origin: null, notAdopted: null };
  const p = plan as Record<string, unknown>;
  const o = p.verify_origin;
  return {
    origin: o === 'user' || o === 'leader' || o === 'approved' ? o : null,
    notAdopted: typeof p.verify_not_adopted === 'string' && p.verify_not_adopted ? p.verify_not_adopted : null,
  };
}
