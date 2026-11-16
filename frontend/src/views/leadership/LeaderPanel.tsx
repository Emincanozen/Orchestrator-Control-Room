import { ArrowRight, Check, ShieldCheck } from 'lucide-react';
import { useState } from 'react';
import { ApiError } from '../../api/client';
import { api } from '../../api/endpoints';
import type { Leadership, SwitchResult } from '../../api/types';
import { ConfirmDialog, Section } from '../../components/ui';
import { useStore } from '../../store/store';
import { formatDateTime } from '../../lib/time';
import { CandidatePicker, candKey } from './CandidatePicker';

export const SWITCH_STEPS = [
  'Pause assigning new tasks (running workers are not touched)',
  'Save the coordination state and create a leadership checkpoint (tasks, approvals, objectives, decisions)',
  'Prepare a concise handoff for the new leader',
  'Set the new leader and validate it: it must restate every open task with a next action (up to 2 attempts)',
  'If validation fails or errors: roll back to the previous leader and report why',
  'Resume assigning tasks',
] as const;

const modelName = (i: { provider: string; model: string | null } | null | undefined) => (i ? `${i.provider} / ${i.model ?? 'default'}` : 'none');

export function LeaderPanel({ l, onChanged }: { l: Leadership; onChanged: () => void }) {
  const leaderState = useStore((s) => s.data.leaderState);
  const [pick, setPick] = useState('');
  const [confirming, setConfirming] = useState(false);
  const [skip, setSkip] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [result, setResult] = useState<SwitchResult | null>(null);

  const cands = l.candidates.leader;
  const chosen = cands.find((c) => candKey(c) === pick) ?? null;
  const same = chosen && l.leader && chosen.provider === l.leader.provider && chosen.model === l.leader.model;
  const open = () => {
    setResult(null);
    setError(null);
    setSkip(false);
    setConfirming(true);
  };
  const close = () => {
    setConfirming(false);
    if (result) setPick('');
    onChanged();
  };
  const run = async () => {
    if (!chosen) return;
    setBusy(true);
    setError(null);
    try {
      const body: { provider: string; model: string; skip_validation?: boolean } = { provider: chosen.provider, model: chosen.model };
      if (skip) body.skip_validation = true;
      setResult(await api.switchLeader(body));
    } catch (e) {
      // 409 leader_switch_failed carries the rollback result in details
      if (e instanceof ApiError && e.code === 'leader_switch_failed' && (e.details as Partial<SwitchResult>).status === 'rolled_back') {
        setResult(e.details as unknown as SwitchResult);
      } else setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Section title="Leader" aria="Leader">
      <div className="grid gap-4 md:grid-cols-2">
        <div>
          <div className="label mb-1">Current leader</div>
          {l.leader ? (
            <div data-testid="current-leader">
              <div className="font-mono text-base text-phosphor">{modelName(l.leader)}</div>
              <dl className="mt-1 grid grid-cols-[auto_1fr] gap-x-3 text-xs text-muted">
                <dt>tier</dt>
                <dd>{l.leader.tier ?? 'unknown'}</dd>
                <dt>since</dt>
                <dd>{l.leader.set_at ? formatDateTime(l.leader.set_at) : 'unknown'}</dd>
                <dt>how</dt>
                <dd>{l.leader.source ?? 'unknown'}</dd>
                <dt>reason</dt>
                <dd data-testid="leader-reason">{l.leader.reason ?? 'not recorded'}</dd>
              </dl>
            </div>
          ) : (
            <div className="text-sm text-amber" data-testid="current-leader">
              {leaderState === 'detecting'
                ? 'Detecting the default leader: the providers are still being checked. This is not an error; the leader appears here once detection finishes.'
                : 'No leader configured: no verified, non-metered model was eligible. Pick one on the right.'}
            </div>
          )}
          <p className="mt-2 text-[11px] text-faint">Minimum tier for the leader role: {l.role_minimum_tier.leader ?? 'unknown'}</p>
        </div>
        <div className="flex flex-col gap-2">
          <label htmlFor="leader-pick" className="label">
            Switch leader to
          </label>
          <CandidatePicker id="leader-pick" label="Candidate leader model" candidates={cands} value={pick} onChange={setPick} emptyLabel="Choose a model..." />
          <div className="flex flex-wrap items-center gap-2">
            <button className="btn btn-primary" disabled={!chosen || !!same || l.switch_in_progress} onClick={open}>
              <ArrowRight className="size-3.5" aria-hidden /> Switch leader...
            </button>
            {same ? <span className="text-xs text-faint">already the leader</span> : null}
            {l.switch_in_progress ? <span className="text-xs text-amber">a switch is already in progress</span> : null}
            {l.assignment_paused ? <span className="chip text-amber">assignment paused</span> : null}
          </div>
          <p className="text-[11px] text-faint">
            Models the router would refuse (not installed, not authenticated, below the minimum tier...) stay listed but disabled, with the reason.
          </p>
        </div>
      </div>

      {confirming && chosen && (
        <ConfirmDialog
          title="Switch leader"
          description={`${modelName(l.leader)}  ->  ${chosen.provider} / ${chosen.model}`}
          confirmLabel="Switch leader"
          busy={busy}
          error={error}
          hideActions={!!result}
          onConfirm={run}
          onCancel={close}
          body={
            <div className="flex flex-col gap-3 text-sm">
              {!result && (
                <>
                  <p>The backend performs these steps in order:</p>
                  <ol className="list-decimal space-y-1 pl-5 text-[13px]" aria-label="Switch steps">
                    {SWITCH_STEPS.map((s) => (
                      <li key={s}>{s}</li>
                    ))}
                  </ol>
                  <p className="rounded-pixel border border-line bg-sunken p-2 text-xs text-muted">
                    Running workers are <b>unaffected</b>: no run is stopped, paused or restarted. Only new assignments wait for a few seconds.
                  </p>
                  {chosen.provider === 'mock' && (
                    <label className="flex items-center gap-2 text-xs">
                      <input type="checkbox" checked={skip} onChange={(e) => setSkip(e.target.checked)} />
                      Skip validation (mock / local providers only; recorded in the event)
                    </label>
                  )}
                  {busy && (
                    <div role="status" className="text-phosphor">
                      Switching: assignment paused, checkpointing, validating with the new leader...
                    </div>
                  )}
                </>
              )}
              {result?.status === 'switched' && (
                <div role="status" data-testid="switch-result" data-status="switched" className="flex flex-col gap-1">
                  <div className="flex items-center gap-2 text-ok">
                    <Check className="size-4" aria-hidden /> Switched to {modelName(result.to)}
                  </div>
                  <div className="text-xs text-muted">
                    {result.validated ? `Validated by the new leader (attempt ${result.attempts}).` : 'Not validated (validation skipped).'}
                    {result.checkpoint_id ? ` Checkpoint ${result.checkpoint_id}.` : ''}
                  </div>
                </div>
              )}
              {result?.status === 'rolled_back' && (
                <div role="alert" data-testid="switch-result" data-status="rolled_back" className="flex flex-col gap-1 rounded-pixel border border-amber/60 bg-amber/10 p-3">
                  <div className="flex items-center gap-2 text-amber">
                    <ShieldCheck className="size-4" aria-hidden /> Rolled back: {modelName(result.from)} is still the leader
                  </div>
                  <div className="text-xs">
                    {modelName(result.to)} could not validate the coordination state after {result.attempts} attempt{result.attempts === 1 ? '' : 's'}.
                  </div>
                  <div className="text-xs text-muted" data-testid="rollback-reason">
                    Reason: {result.reason}
                  </div>
                </div>
              )}
            </div>
          }
        />
      )}
    </Section>
  );
}
