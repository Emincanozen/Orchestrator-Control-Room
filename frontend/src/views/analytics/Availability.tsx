import type { Metric, ProviderAvailability, SizeDistribution } from '../../api/types';
import { ExactnessChip } from '../../components/ui';
import { pct } from '../../lib/analytics';

/**
 * Per-provider availability. `available_share` is null when every observation was UNKNOWN (no quota data):
 * that is shown as "unknown", never as 0%. Known and unknown observation counts are always shown.
 */
export function AvailabilityList({ value }: { value: Record<string, ProviderAvailability> }) {
  return (
    <ul className="flex flex-col gap-1 text-sm" data-testid="availability-list">
      {Object.entries(value).map(([p, a]) => {
        const known = a.known_observations ?? a.observations;
        const unknown = a.unknown_observations ?? 0;
        return (
          <li key={p} data-testid={`availability-${p}`} className="flex flex-wrap items-center gap-x-2">
            <span className="font-mono">{p}</span>
            <span data-testid="availability-share">{a.available_share === null || a.available_share === undefined ? 'unknown' : pct(a.available_share)}</span>
            <span className="text-[11px] text-muted" data-testid="availability-counts">
              ({known} known, {unknown} unknown of {a.observations} obs)
            </span>
            {a.exactness && <ExactnessChip exactness={a.exactness} />}
          </li>
        );
      })}
    </ul>
  );
}

const KIND_LABEL: Record<string, string> = { prompt_chars: 'prompt', handoff_chars: 'handoff', knowledge_chars: 'knowledge' };

function isDist(v: unknown): v is SizeDistribution {
  return !!v && typeof v === 'object' && 'p50' in v && 'max' in v;
}

/** `handoff_prompt_chars.value`: {prompt_chars, handoff_chars, knowledge_chars}, each {p50, p90, mean, max} or null. A flat distribution is accepted too. */
export function PromptSizes({ value }: { value: Metric<unknown>['value'] }) {
  const rows: [string, SizeDistribution | null][] = isDist(value)
    ? [['prompt', value]]
    : Object.entries((value ?? {}) as Record<string, SizeDistribution | null>).map(([k, d]) => [KIND_LABEL[k] ?? k, isDist(d) ? d : null]);
  return (
    <table className="text-[11px]" data-testid="prompt-sizes">
      <thead>
        <tr className="text-faint">
          <th className="pr-2 text-left font-normal">chars</th>
          <th className="pr-2 text-right font-normal">p50</th>
          <th className="pr-2 text-right font-normal">p90</th>
          <th className="pr-2 text-right font-normal">mean</th>
          <th className="text-right font-normal">max</th>
        </tr>
      </thead>
      <tbody>
        {rows.map(([k, d]) => (
          <tr key={k} data-testid={`prompt-size-${k}`}>
            <td className="pr-2">{k}</td>
            {d ? (
              ['p50', 'p90', 'mean', 'max'].map((f) => (
                <td key={f} className="tick-num pr-2 text-right">
                  {(d as unknown as Record<string, number | null>)[f] ?? 'unknown'}
                </td>
              ))
            ) : (
              <td colSpan={4} className="text-faint">
                not recorded
              </td>
            )}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
