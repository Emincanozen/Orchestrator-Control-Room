import * as Dialog from '@radix-ui/react-dialog';
import { AlertTriangle, Loader2, Plug, X } from 'lucide-react';
import type { ReactNode } from 'react';
import { ACTIVITY_COLOR, STATE_COLOR, STATE_LABEL, activityLabel } from '../design/tokens';
import type { AgentActivity, Measured, TaskState } from '../api/types';
import { ApiError } from '../api/client';
import { formatDuration } from '../lib/time';
import type { Resource } from '../lib/useResource';

export function StateBadge({ state }: { state: TaskState }) {
  return (
    <span className="chip" style={{ color: STATE_COLOR[state] ?? 'var(--color-muted)' }} data-state={state}>
      <span aria-hidden className={`inline-block size-1.5 ${state === 'RUNNING' ? 'pulse-dot' : ''}`} style={{ background: 'currentColor' }} />
      {STATE_LABEL[state] ?? state}
    </span>
  );
}

export function ActivityBadge({ activity }: { activity: AgentActivity }) {
  return (
    <span className="chip" style={{ color: ACTIVITY_COLOR[activity] ?? 'var(--color-muted)' }}>
      <span aria-hidden className="inline-block size-1.5" style={{ background: 'currentColor' }} />
      {activityLabel(activity)}
    </span>
  );
}

/** Visible marker for values that are not exact measurements. */
export function ExactnessChip({ exactness }: { exactness: string }) {
  if (exactness === 'exact' || exactness === 'measured') {
    return (
      <span className="chip text-ok" title={`Exactness: ${exactness}`}>
        {exactness}
      </span>
    );
  }
  const color = exactness === 'estimated' || exactness === 'ai_evaluated' ? 'text-amber' : 'text-faint';
  return (
    <span className={`chip ${color}`} title={`Exactness: ${exactness}`}>
      {exactness}
    </span>
  );
}

export function MeasuredValue({ m, unit, format }: { m: Measured | null | undefined; unit?: string; format?: (n: number) => string }) {
  if (!m || m.value === null || m.value === undefined) {
    return (
      <span className="inline-flex items-center gap-1.5">
        <span className="text-faint">unknown</span>
        <ExactnessChip exactness={m?.exactness ?? 'unknown'} />
      </span>
    );
  }
  const text = format ? format(m.value) : String(m.value);
  return (
    <span className="inline-flex items-center gap-1.5" title={m.source ? `source: ${m.source}` : undefined}>
      <span className="tick-num">
        {text}
        {unit ? ` ${unit}` : ''}
      </span>
      <ExactnessChip exactness={m.exactness} />
    </span>
  );
}

export function Duration({ seconds }: { seconds: number | null }) {
  return <span className="tick-num">{formatDuration(seconds)}</span>;
}

export function Spinner({ label = 'Loading' }: { label?: string }) {
  return (
    <span role="status" className="inline-flex items-center gap-2 text-muted">
      <Loader2 className="size-4 animate-spin" aria-hidden />
      {label}
    </span>
  );
}

export function Empty({ title, hint, children }: { title: string; hint?: string; children?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-1 px-4 py-10 text-center" data-testid="empty">
      <div className="display text-sm text-muted">{title}</div>
      {hint && <div className="max-w-md text-xs text-faint">{hint}</div>}
      {children}
    </div>
  );
}

/** Shown when the backend endpoint answers 404/501: never fabricate data. */
export function Unavailable({ what }: { what: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-4 py-12 text-center" data-testid="unavailable">
      <Plug className="size-6 text-faint" aria-hidden />
      <div className="display text-sm text-muted">{what} not available yet</div>
      <div className="max-w-md text-xs text-faint">
        The backend has not implemented this endpoint in the running version. Nothing is shown rather than made-up data.
      </div>
    </div>
  );
}

export function ErrorBox({ error }: { error: unknown }) {
  const msg = error instanceof ApiError ? `${error.message} (${error.code})` : error instanceof Error ? error.message : String(error);
  return (
    <div role="alert" className="flex items-start gap-2 rounded-pixel border border-danger/60 bg-danger/10 px-3 py-2 text-sm text-danger">
      <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
      <span>{msg}</span>
    </div>
  );
}

export function Modal({
  open,
  onOpenChange,
  title,
  description,
  children,
  side = 'center',
  wide = false,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  title: string;
  description?: string;
  children: ReactNode;
  side?: 'center' | 'right';
  wide?: boolean;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/60 backdrop-blur-[1px]" />
        <Dialog.Content
          className={
            side === 'right'
              ? 'pixel-card fixed inset-y-0 right-0 z-50 flex w-[min(52rem,96vw)] flex-col'
              : `pixel-card fixed left-1/2 top-1/2 z-50 flex max-h-[90vh] -translate-x-1/2 -translate-y-1/2 flex-col ${wide ? 'w-[min(52rem,96vw)]' : 'w-[min(34rem,96vw)]'}`
          }
        >
          <div className="flex items-start justify-between gap-3 border-b border-line px-4 py-3">
            <div className="min-w-0">
              <Dialog.Title className="display truncate text-base text-ink">{title}</Dialog.Title>
              {description ? (
                <Dialog.Description className="text-xs text-muted">{description}</Dialog.Description>
              ) : (
                <Dialog.Description className="sr-only">{title}</Dialog.Description>
              )}
            </div>
            <Dialog.Close className="btn btn-ghost btn-sm" aria-label="Close">
              <X className="size-4" aria-hidden />
            </Dialog.Close>
          </div>
          <div className="min-h-0 flex-1 overflow-auto">{children}</div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export function Field({ label, htmlFor, hint, children }: { label: string; htmlFor?: string; hint?: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={htmlFor} className="label">
        {label}
      </label>
      {children}
      {hint && <span className="text-[11px] text-faint">{hint}</span>}
    </div>
  );
}

export function ViewHeader({ title, subtitle, actions }: { title: string; subtitle?: string; actions?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-3 px-5 pb-3 pt-5">
      <div>
        <h1 className="display text-lg text-ink">{title}</h1>
        {subtitle && <p className="text-xs text-muted">{subtitle}</p>}
      </div>
      <div className="flex items-center gap-2">{actions}</div>
    </div>
  );
}

/** Standard loading / unavailable / error / data switch for a useResource result. */
export function Async<T>({ res, what, children }: { res: Resource<T>; what: string; children: (d: T) => ReactNode }) {
  if (res.unavailable) return <Unavailable what={what} />;
  if (res.error)
    return (
      <div className="p-4">
        <ErrorBox error={res.error} />
      </div>
    );
  if (res.data === null) return <div className="p-4">{res.loading ? <Spinner /> : <Empty title={`No ${what.toLowerCase()}`} />}</div>;
  return <>{children(res.data)}</>;
}

export function JsonBlock({ value }: { value: unknown }) {
  return <pre className="max-h-96 overflow-auto whitespace-pre-wrap bg-void p-3 font-mono text-[11px]">{JSON.stringify(value, null, 2)}</pre>;
}

/** Confirmation modal with a body, an optional error and a busy state. */
export function ConfirmDialog({
  title,
  description,
  body,
  confirmLabel,
  danger = false,
  busy = false,
  error,
  onConfirm,
  onCancel,
  hideActions = false,
}: {
  title: string;
  description?: string;
  body: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  busy?: boolean;
  error?: unknown;
  onConfirm: () => void;
  onCancel: () => void;
  /** after a result is shown the caller replaces the actions by a single Close button */
  hideActions?: boolean;
}) {
  return (
    <Modal open onOpenChange={(o) => !o && !busy && onCancel()} title={title} description={description}>
      <div className="flex flex-col gap-4 p-4">
        {body}
        {error ? <ErrorBox error={error} /> : null}
        <div className="flex justify-end gap-2">
          {hideActions ? (
            <button className="btn" onClick={onCancel}>
              Close
            </button>
          ) : (
            <>
              <button className="btn" onClick={onCancel} disabled={busy}>
                Cancel
              </button>
              <button className={`btn ${danger ? 'btn-danger' : 'btn-primary'}`} onClick={onConfirm} disabled={busy}>
                {busy ? 'Working…' : confirmLabel}
              </button>
            </>
          )}
        </div>
      </div>
    </Modal>
  );
}

/** Small section wrapper used by the M4d views. */
export function Section({ title, aria, actions, children }: { title: string; aria?: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <section aria-label={aria ?? title} className="pixel-card flex flex-col gap-3 p-4">
      <div className="flex items-center justify-between gap-3">
        <h2 className="label">{title}</h2>
        {actions}
      </div>
      {children}
    </section>
  );
}
