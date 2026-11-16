import type { LeaderCandidate } from '../../api/types';

export const candKey = (c: { provider: string; model: string | null }): string => `${c.provider}::${c.model ?? ''}`;

export function candidateLabel(c: LeaderCandidate): string {
  const base = `${c.provider} / ${c.model}${c.tier ? ` (${c.tier})` : ''}`;
  return c.eligible ? base : `${base} - not eligible: ${c.reasons.join('; ') || 'unknown reason'}`;
}

/**
 * Native select over the backend's candidates. Ineligible models stay in the list, disabled, with the
 * backend's reason in the option text (they are never hidden).
 */
export function CandidatePicker({
  id,
  label,
  candidates,
  value,
  onChange,
  emptyLabel,
}: {
  id: string;
  label: string;
  candidates: LeaderCandidate[];
  value: string;
  onChange: (key: string) => void;
  emptyLabel: string;
}) {
  return (
    <select id={id} className="field" aria-label={label} value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">{emptyLabel}</option>
      {candidates.map((c) => (
        <option key={candKey(c)} value={candKey(c)} disabled={!c.eligible} data-eligible={c.eligible}>
          {candidateLabel(c)}
        </option>
      ))}
    </select>
  );
}
