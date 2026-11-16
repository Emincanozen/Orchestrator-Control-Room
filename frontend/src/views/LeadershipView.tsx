import { RefreshCw } from 'lucide-react';
import { api } from '../api/endpoints';
import { Async, ViewHeader } from '../components/ui';
import { useResource } from '../lib/useResource';
import { useStore } from '../store/store';
import { ConsultPanel } from './leadership/ConsultPanel';
import { DecisionsPanel } from './leadership/DecisionsPanel';
import { LeaderPanel } from './leadership/LeaderPanel';
import { AdvisorPanel, BudgetsPanel, PermissionsPanel } from './leadership/SettingsPanel';

/** Leader / advisor settings, safe switch, advisor consults and decision records (all from the real API). */
export function LeadershipView() {
  const rev = useStore((s) => s.data.rev);
  const leadership = useResource(() => api.leadership(), [rev.leadership]);
  const decisions = useResource(() => api.decisions(), [rev.decisions]);
  const reload = () => {
    leadership.reload();
    decisions.reload();
  };
  return (
    <div>
      <ViewHeader
        title="Leadership"
        subtitle="Who leads and who advises, the safe switch, budgets, advisor consults and decision records"
        actions={
          <button className="btn btn-sm" onClick={reload} aria-label="Refresh leadership">
            <RefreshCw className="size-3.5" aria-hidden /> Refresh
          </button>
        }
      />
      <div className="flex flex-col gap-4 px-5 pb-8">
        <Async res={leadership} what="Leadership">
          {(l) => (
            <>
              <LeaderPanel l={l} onChanged={leadership.reload} />
              <div className="grid gap-4 xl:grid-cols-2">
                <AdvisorPanel l={l} onChanged={leadership.reload} />
                <BudgetsPanel l={l} onChanged={leadership.reload} />
              </div>
              <PermissionsPanel l={l} />
            </>
          )}
        </Async>
        <ConsultPanel onConsulted={reload} />
        <Async res={decisions} what="Decisions">
          {(d) => <DecisionsPanel decisions={d} onChanged={decisions.reload} />}
        </Async>
      </div>
    </div>
  );
}
