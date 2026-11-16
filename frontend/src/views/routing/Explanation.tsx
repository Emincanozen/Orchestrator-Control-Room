import type { RankedCandidate, RejectedCandidate, RouterDecision, RoutingDetail, RoutingOutcome, TaskClassification } from '../../api/types';
import { COMPONENT_LABEL, OUTCOME_TEXT, filterText } from '../../lib/explain';
import { formatDateTime } from '../../lib/time';
import { ResetCountdown } from '../providers/parts';

/** What the explanation panel renders; built from a persisted decision or from a preview response. */
export interface ExplainInput {
  outcome?: RoutingOutcome;
  chosen: { provider: string; model: string | null } | null;
  classification?: TaskClassification;
  decision?: RouterDecision;
  candidates: RankedCandidate[];
  reasons: string[];
  ruleIds: string[];
  adaptive: boolean;
  fallback?: RoutingDetail['fallback'];
}

const SEGMENT_COLORS = ['var(--color-phosphor)', 'var(--color-signal)', 'var(--color-amber)', 'var(--color-st-planning)', 'var(--color-st-reviewing)'];

export function ScoreBar({ components }: { components: Record<string, number> }) {
  const entries = Object.entries(components);
  const total = entries.reduce((a, [, v]) => a + Math.max(0, v), 0);
  const label = entries.map(([k, v]) => `${COMPONENT_LABEL[k] ?? k} ${v.toFixed(1)}`).join(', ');
  return (
    <div className="flex flex-col gap-1">
      <div role="img" aria-label={`Score breakdown: ${label}`} className="flex h-2 w-full min-w-32 max-w-md overflow-hidden border border-line bg-void">
        {total > 0 &&
          entries.map(([k, v], i) =>
            v > 0 ? <span key={k} title={`${COMPONENT_LABEL[k] ?? k}: ${v.toFixed(2)}`} style={{ width: `${(v / total) * 100}%`, background: SEGMENT_COLORS[i % SEGMENT_COLORS.length] }} /> : null,
          )}
      </div>
      <ul className="flex flex-wrap gap-x-3 text-[11px] text-muted">
        {entries.map(([k, v], i) => (
          <li key={k} className="tick-num">
            <span aria-hidden className="mr-1 inline-block size-1.5" style={{ background: SEGMENT_COLORS[i % SEGMENT_COLORS.length] }} />
            {COMPONENT_LABEL[k] ?? k} {v.toFixed(1)}
          </li>
        ))}
      </ul>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-1.5">
      <h4 className="label">{title}</h4>
      {children}
    </section>
  );
}

function Chips({ items, empty = 'none' }: { items: string[]; empty?: string }) {
  if (!items.length) return <span className="text-xs text-faint">{empty}</span>;
  return (
    <span className="flex flex-wrap gap-1">
      {items.map((c) => (
        <span key={c} className="chip text-muted">
          {c}
        </span>
      ))}
    </span>
  );
}

function RejectedRow({ r }: { r: RejectedCandidate }) {
  const filters = r.filters?.length ? r.filters : [r.filter];
  return (
    <li className="border-t border-line/60 py-1.5 text-xs" data-testid="rejected-candidate">
      <span className="font-mono">
        {r.provider} / {r.model}
      </span>
      <ul className="ml-3 list-disc text-muted">
        {filters.map((f) => (
          <li key={f} data-filter={f}>
            {filterText(f)}
          </li>
        ))}
      </ul>
      {r.detail && <div className="ml-3 text-[11px] text-faint">{r.detail}</div>}
    </li>
  );
}

export function Explanation({ x }: { x: ExplainInput }) {
  const d = x.decision;
  const cls = x.classification;
  const rejected = d?.rejected ?? [];
  const adjustments = x.candidates.filter((c) => c.adjustment_explanation);
  const outcome = x.outcome ? OUTCOME_TEXT[x.outcome] : undefined;
  const noCandidate = x.chosen === null;
  return (
    <div className="flex flex-col gap-4 border-t border-line p-3" data-testid="explanation">
      {outcome && (
        <div className="flex flex-wrap items-center gap-2" data-testid="outcome" data-outcome={x.outcome}>
          <span className="chip" style={{ color: outcome.color }}>
            {outcome.label}
          </span>
          <span className="text-xs text-muted">{outcome.text}</span>
        </div>
      )}
      {x.chosen && (
        <p className="text-[13px]">
          Chosen: <span className="font-mono text-phosphor">{x.chosen.provider} / {x.chosen.model ?? 'default model'}</span>
          {d?.would_route_to && <span className="ml-2 text-xs text-muted">(would route to {d.would_route_to})</span>}
        </p>
      )}
      {noCandidate && (
        <div role="note" className="rounded-pixel border border-amber/50 bg-amber/10 px-3 py-2 text-xs" data-testid="no-candidate">
          <div className="text-amber">No model was selected.</div>
          <div className="text-ink">{d?.no_candidate_reason ?? 'The router gave no reason.'}</div>
          {d?.wait_for_quota && d.resets_at && (
            <div className="mt-1">
              Earliest reset: {formatDateTime(d.resets_at)} <ResetCountdown iso={d.resets_at} />
            </div>
          )}
        </div>
      )}

      {cls && (
        <Section title="Classification">
          <dl className="grid grid-cols-[8rem_1fr] gap-x-3 gap-y-1 text-xs">
            <dt className="text-muted">Type</dt>
            <dd className="font-mono" data-testid="cls-type">{cls.task_type}</dd>
            <dt className="text-muted">Complexity</dt>
            <dd className="font-mono">{cls.complexity}</dd>
            <dt className="text-muted">Risk</dt>
            <dd className="font-mono">{cls.risk}</dd>
            <dt className="text-muted">Required capabilities</dt>
            <dd>
              <Chips items={cls.required_capabilities} />
            </dd>
            <dt className="text-muted">Signals</dt>
            <dd>
              {cls.signals.length ? (
                <ul className="list-inside list-disc text-muted">
                  {cls.signals.map((s, i) => (
                    <li key={i}>{s}</li>
                  ))}
                </ul>
              ) : (
                <span className="text-faint">none</span>
              )}
            </dd>
            {d?.min_tier && (
              <>
                <dt className="text-muted">Tier needed</dt>
                <dd className="font-mono">
                  at least {d.min_tier}, aiming for {d.target_tier}
                  {d.role ? ` (role ${d.role})` : ''}
                </dd>
              </>
            )}
          </dl>
        </Section>
      )}

      <Section title={`Ranked candidates (${x.candidates.length})`}>
        {x.candidates.length === 0 ? (
          <p className="text-xs text-faint">No candidate passed the filters.</p>
        ) : (
          <ol className="flex flex-col gap-2" aria-label="Ranked candidates">
            {x.candidates.map((c) => (
              <li key={`${c.provider}/${c.model}`} className="grid grid-cols-[2rem_1fr_5rem] items-start gap-2 text-xs" data-testid="candidate">
                <span className="display text-muted">#{c.rank}</span>
                <div className="min-w-0">
                  <div className="font-mono">
                    {c.provider} / {c.model} <span className="text-faint">· {c.tier}</span>
                    {c.usage_state && <span className="ml-1 text-faint">· usage {c.usage_state.toLowerCase()}</span>}
                  </div>
                  <ScoreBar components={c.components} />
                  {c.adjustment_explanation && <div className="text-[11px] text-amber">{c.adjustment_explanation}</div>}
                </div>
                <span className="tick-num text-right text-ink" title="Total score">
                  {c.score.toFixed(1)}
                </span>
              </li>
            ))}
          </ol>
        )}
      </Section>

      {rejected.length > 0 && (
        <Section title={`Rejected (${rejected.length})`}>
          <ul aria-label="Rejected candidates">
            {rejected.map((r) => (
              <RejectedRow key={`${r.provider}/${r.model}`} r={r} />
            ))}
          </ul>
        </Section>
      )}

      <Section title="Why">
        {x.reasons.length ? (
          <ul className="list-inside list-disc text-xs text-muted" aria-label="Reasons">
            {x.reasons.map((r, i) => (
              <li key={i}>{r}</li>
            ))}
          </ul>
        ) : (
          <p className="text-xs text-faint">No reasons recorded.</p>
        )}
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className="text-muted">Rules:</span>
          <Chips items={x.ruleIds} />
        </div>
        <div className="flex flex-wrap items-center gap-2 text-xs" data-testid="adaptive">
          <span className="text-muted">Adaptive routing:</span>
          {x.adaptive ? <span className="chip text-amber">applied</span> : <span className="chip text-faint">not applied</span>}
          {adjustments.length === 0 && !x.adaptive && <span className="text-faint">Not enough verified history yet; scores use configured weights only.</span>}
        </div>
        {adjustments.map((c) => (
          <p key={`${c.provider}/${c.model}`} className="text-[11px] text-muted">
            {c.provider}/{c.model}: {c.adjustment_explanation}
          </p>
        ))}
        {d?.requires_approval && <p className="text-xs text-amber">Requires your approval before it runs.</p>}
        {d?.extra_verification && <p className="text-xs text-amber">Extra verification is required for this task.</p>}
      </Section>

      {x.fallback && (
        <Section title="Fallback">
          <p className="text-xs" data-testid="fallback">
            <span className="chip text-signal">{x.fallback.action}</span> {x.fallback.category && <span className="text-muted">after {x.fallback.category}: </span>}
            {x.fallback.reason}
          </p>
        </Section>
      )}
    </div>
  );
}
