import { RefreshCw } from 'lucide-react';
import { useMemo, useState } from 'react';
import { api } from '../api/endpoints';
import type { AnalyticsFilters, AnalyticsSummary } from '../api/types';
import { Async, ErrorBox, Section, Unavailable, ViewHeader } from '../components/ui';
import { TASK_TYPES } from '../lib/constants';
import { formatSeconds, isMissing, num, pct, usd } from '../lib/analytics';
import { useResource } from '../lib/useResource';
import { useStore } from '../store/store';
import { AdaptivePanel } from './analytics/Adaptive';
import { DurationChart, FailureChart, TimeseriesChart, TokensChart } from './analytics/Charts';
import { AvailabilityList, PromptSizes } from './analytics/Availability';
import { KpiTile } from './analytics/Kpi';
import { AgentTable, ModelTable } from './analytics/Tables';

function Tiles({ s }: { s: AnalyticsSummary['metrics'] }) {
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4" aria-label="Key figures">
      <KpiTile id="tasks_completed" title="Tasks completed" metric={s.tasks_completed} render={(v) => v} note="n = tasks in scope" />
      <KpiTile id="verified_completion_rate" title="Verified completion rate" metric={s.verified_completion_rate} isRate render={(v) => pct(v)} note="completed with a passed verification / finished tasks" />
      <KpiTile id="test_pass_rate" title="Test pass rate" metric={s.test_pass_rate} isRate render={(v) => pct(v)} note="passed verifications / all verifications" />
      <KpiTile id="retries_per_task" title="Retries per task" metric={s.retries_per_task} render={(v) => num(v)} />
      <KpiTile id="retries_total" title="Retries (total)" metric={s.retries_total} render={(v) => v} />
      <KpiTile id="repair_cycles_total" title="Repair cycles (total)" metric={s.repair_cycles_total} render={(v) => v} />
      <KpiTile
        id="provider_model_switches"
        title="Provider/model switches"
        metric={s.provider_model_switches}
        render={(v) => `${v.total}`}
        extra={undefined}
        note="consecutive runs of one task on a different model"
      />
      <KpiTile id="task_active_seconds" title="Task active time (median)" metric={s.task_active_seconds} render={(v) => `${formatSeconds(v.p50)}`} note="p50 of tasks with active time" />
      <KpiTile id="cost_usd_estimated" title="Estimated cost" metric={s.cost_usd_estimated} render={(v) => usd(v)} note="estimated: Claude list-price equivalent, not billed" />
      <KpiTile id="handoff_checkpoints" title="Handoff checkpoints" metric={s.handoff_checkpoints} render={(v) => v} />
      <KpiTile
        id="provider_availability"
        title="Provider availability"
        metric={s.provider_availability}
        render={(v) => <AvailabilityList value={v} />}
        extra={isMissing(s.provider_availability) && s.provider_availability?.value ? <AvailabilityList value={s.provider_availability.value} /> : undefined}
        note="share of KNOWN sampled observations in state AVAILABLE or LOW; UNKNOWN observations are counted separately and never as unavailable; not time-weighted"
      />
      <KpiTile id="handoff_prompt_chars" title="Handoff prompt size" metric={s.handoff_prompt_chars} render={(v) => <PromptSizes value={v} />} />
    </div>
  );
}

export function AnalyticsView() {
  const projects = useStore((s) => s.data.projects);
  const rev = useStore((s) => s.data.rev);
  const [f, setF] = useState<AnalyticsFilters>({});
  const [metric, setMetric] = useState('tasks_completed');
  const set = (k: keyof AnalyticsFilters, v: string) => setF((cur) => ({ ...cur, [k]: v || undefined }));
  const key = JSON.stringify(f);

  const summary = useResource(() => api.analyticsSummary(f), [key, rev.routing, rev.usage]);
  const series = useResource(() => api.analyticsTimeseries(metric, f), [key, metric, rev.routing]);
  const models = useResource(() => api.analyticsModels(), [rev.routing, rev.usage]);
  const routing = useResource(() => api.analyticsRouting(), [rev.routing]);
  const agents = useResource(() => api.analyticsAgents(f), [key, rev.routing]);

  const providers = useMemo(() => [...new Set((models.data?.models ?? []).map((m) => m.provider))].sort(), [models.data]);
  const modelNames = useMemo(() => [...new Set((models.data?.models ?? []).filter((m) => !f.provider || m.provider === f.provider).map((m) => m.model))].sort(), [models.data, f.provider]);
  const reload = () => [summary, series, models, routing, agents].forEach((r) => r.reload());

  if (summary.unavailable) return (<div><ViewHeader title="Analytics" /><Unavailable what="Analytics" /></div>);

  return (
    <div>
      <ViewHeader
        title="Analytics"
        subtitle="Measured from recorded tasks, runs and verifications. Missing data stays missing; it is never shown as zero."
        actions={
          <button className="btn btn-sm" onClick={reload} aria-label="Refresh analytics">
            <RefreshCw className="size-3.5" aria-hidden /> Refresh
          </button>
        }
      />
      <div className="flex flex-col gap-4 px-5 pb-8">
        <form className="flex flex-wrap items-end gap-3" aria-label="Analytics filters" onSubmit={(e) => e.preventDefault()}>
          <FilterSelect label="Project" value={f.project_id ?? ''} onChange={(v) => set('project_id', v)} all="All projects" options={Object.values(projects).map((p) => [p.id, p.name])} />
          <FilterSelect label="Provider" value={f.provider ?? ''} onChange={(v) => setF((c) => ({ ...c, provider: v || undefined, model: undefined }))} all="All providers" options={providers.map((p) => [p, p])} />
          <FilterSelect label="Model" value={f.model ?? ''} onChange={(v) => set('model', v)} all="All models" options={modelNames.map((p) => [p, p])} />
          <FilterSelect label="Task type" value={f.task_type ?? ''} onChange={(v) => set('task_type', v)} all="All task types" options={TASK_TYPES.map((t) => [t, t])} />
          <label className="flex flex-col gap-1">
            <span className="label">From</span>
            <input type="date" className="field" aria-label="From date" value={f.from ?? ''} max={f.to} onChange={(e) => set('from', e.target.value)} />
          </label>
          <label className="flex flex-col gap-1">
            <span className="label">To</span>
            <input type="date" className="field" aria-label="To date" value={f.to ?? ''} min={f.from} onChange={(e) => set('to', e.target.value)} />
          </label>
          {Object.keys(f).length > 0 && (
            <button type="button" className="btn btn-sm" onClick={() => setF({})}>
              Clear filters
            </button>
          )}
        </form>

        <Async res={summary} what="Analytics summary">
          {(s) => (
            <>
              <Tiles s={s.metrics} />
              <div className="grid gap-4 xl:grid-cols-2">
                <FailureChart metric={s.metrics.failure_frequency} />
                <DurationChart id="run-duration" title="Run duration (p50 / p90)" metric={s.metrics.run_duration_seconds} />
              </div>
              <TokensChart tokens={s.metrics.tokens} cost={s.metrics.cost_usd_estimated} />
            </>
          )}
        </Async>

        {series.error && !series.unavailable ? <ErrorBox error={series.error} /> : <TimeseriesChart ts={series.data} metric={metric} onMetric={setMetric} />}

        <Section title="Model performance" aria="Model performance">
          <p className="text-[11px] text-faint">{models.data?.definition ?? ''} Success rate = successful / verified execute and repair runs, with the sample size n next to it.</p>
          <Async res={models} what="Model performance">
            {(m) => <ModelTable rows={m.models} minSamples={routing.data?.settings.min_samples ?? null} />}
          </Async>
        </Section>

        <Async res={routing} what="Adaptive routing">
          {(r) => <AdaptivePanel routing={r} onSaved={routing.reload} />}
        </Async>

        <Section title="Agent history" aria="Agent history">
          <Async res={agents} what="Agent history">
            {(a) => <AgentTable rows={a.agents} />}
          </Async>
        </Section>
      </div>
    </div>
  );
}

function FilterSelect({ label, value, onChange, all, options }: { label: string; value: string; onChange: (v: string) => void; all: string; options: [string, string][] }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="label">{label}</span>
      <select className="field w-auto min-w-32" aria-label={label} value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">{all}</option>
        {options.map(([v, l]) => (
          <option key={v} value={v}>
            {l}
          </option>
        ))}
      </select>
    </label>
  );
}
