import type { VerifyCommand, VerifyOrigin } from '../api/types';
import { VERIFY_ORIGIN_LABEL, verifyKind, verifyToString } from '../lib/verify';

/** A list of verify checks in readable form: a kind chip plus the description. Used on tasks, objectives and approvals. */
export function VerifyChecks({ checks, testId = 'verify-checks' }: { checks: VerifyCommand[]; testId?: string }) {
  return (
    <ul className="flex flex-col gap-0.5 font-mono text-xs" data-testid={testId}>
      {checks.map((c, i) => (
        <li key={i} className="flex flex-wrap items-baseline gap-1.5" data-kind={verifyKind(c)}>
          <span className="chip text-muted">{verifyKind(c)}</span>
          <span className="break-all">{verifyToString(c)}</span>
        </li>
      ))}
    </ul>
  );
}

export function VerifyOriginChip({ origin }: { origin: VerifyOrigin | null | undefined }) {
  if (!origin) return <span className="text-xs text-faint" data-testid="verify-origin">origin not recorded</span>;
  return (
    <span className="chip text-signal" data-testid="verify-origin" data-origin={origin} title={VERIFY_ORIGIN_LABEL[origin]}>
      {origin}
    </span>
  );
}
