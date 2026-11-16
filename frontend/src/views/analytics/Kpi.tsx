import type { ReactNode } from 'react';
import type { Metric } from '../../api/types';
import { ExactnessChip } from '../../components/ui';
import { LOW_EVIDENCE_N, NO_DATA, isMissing } from '../../lib/analytics';

/**
 * One summary metric. EVERY tile shows its exactness and n. A missing metric reads "No data yet", never 0.
 * Rates (isRate) with n below LOW_EVIDENCE_N show "n = ..." prominently plus a low-evidence hint.
 */
export function KpiTile<V>({
  id,
  title,
  metric,
  render,
  isRate = false,
  note,
  extra,
}: {
  id: string;
  title: string;
  metric: Metric<V> | undefined;
  render: (v: NonNullable<V>) => ReactNode;
  isRate?: boolean;
  note?: string;
  extra?: ReactNode;
}) {
  const missing = isMissing(metric);
  const n = metric?.n ?? 0;
  const low = isRate && !missing && n < LOW_EVIDENCE_N;
  return (
    <div className="pixel-card flex flex-col gap-1 p-3" data-testid={`kpi-${id}`} data-exactness={metric?.exactness ?? 'missing'} data-missing={missing}>
      <div className="label">{title}</div>
      {missing ? (
        <div className="text-lg text-faint" data-testid="kpi-value">
          {NO_DATA}
        </div>
      ) : (
        <div className="tick-num text-lg text-ink" data-testid="kpi-value">
          {render(metric!.value as NonNullable<V>)}
        </div>
      )}
      <div className="flex flex-wrap items-center gap-1.5">
        <ExactnessChip exactness={metric?.exactness ?? 'missing'} />
        <span className={low ? 'chip text-amber' : 'text-[11px] text-muted'} data-testid="kpi-n" title={metric?.source}>
          {missing ? 'n = 0' : `n = ${n}`}
        </span>
      </div>
      {low && (
        <div className="text-[11px] text-amber" data-testid="low-evidence">
          Low evidence: fewer than {LOW_EVIDENCE_N} samples. Do not read this as a trend.
        </div>
      )}
      {note && <div className="text-[11px] text-faint">{note}</div>}
      {extra}
    </div>
  );
}
