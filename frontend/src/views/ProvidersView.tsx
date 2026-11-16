import { RefreshCw } from 'lucide-react';
import { useState } from 'react';
import { api } from '../api/endpoints';
import { Async, Empty, ErrorBox, ViewHeader } from '../components/ui';
import { useResource } from '../lib/useResource';
import { useStore } from '../store/store';
import { ModelsTable, ProviderCard, UsageEntryCard } from './providers/parts';

export function ProvidersView() {
  const rev = useStore((s) => s.data.rev);
  // provider.status / usage.updated events bump these counters: the lists refetch.
  const providers = useResource(() => api.providers(), [rev.providers, rev.usage]);
  const models = useResource(() => api.models(), [rev.providers]);
  const usage = useResource(() => api.usage(), [rev.usage, rev.providers]);
  const [refreshError, setRefreshError] = useState<unknown>(null);
  const [refreshing, setRefreshing] = useState(false);

  const refresh = async () => {
    setRefreshing(true);
    setRefreshError(null);
    try {
      await api.refreshProviders();
      providers.reload();
      models.reload();
      usage.reload();
    } catch (e) {
      setRefreshError(e);
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <div>
      <ViewHeader
        title="Providers & Usage"
        subtitle="Detected CLIs, discovered models and quota observations. Unknown stays unknown."
        actions={
          <button className="btn" onClick={refresh} disabled={refreshing}>
            <RefreshCw className={`size-3.5 ${refreshing ? 'animate-spin' : ''}`} aria-hidden /> Refresh
          </button>
        }
      />
      <div className="flex flex-col gap-6 px-5 pb-8">
        {refreshError ? <ErrorBox error={refreshError} /> : null}
        <section aria-label="Providers">
          <h2 className="label mb-2">Providers</h2>
          <Async res={providers} what="Providers">
            {(rows) =>
              rows.length === 0 ? (
                <Empty title="No providers reported" />
              ) : (
                <div className="grid grid-cols-[repeat(auto-fill,minmax(24rem,1fr))] gap-3">
                  {rows.map((p) => (
                    <ProviderCard key={p.provider} p={p} />
                  ))}
                </div>
              )
            }
          </Async>
        </section>
        <section aria-label="Usage">
          <h2 className="label mb-2">Usage observations</h2>
          <Async res={usage} what="Usage">
            {(rows) =>
              rows.length === 0 ? (
                <Empty title="No usage reported" hint="Usage appears after real runs report tokens or rate-limit windows." />
              ) : (
                <div className="flex flex-col gap-3">
                  {rows.map((e) => (
                    <UsageEntryCard key={`${e.provider}/${e.model ?? ''}`} e={e} />
                  ))}
                </div>
              )
            }
          </Async>
        </section>
        <section aria-label="Models">
          <h2 className="label mb-2">Models</h2>
          <Async res={models} what="Models">
            {(rows) => (rows.length === 0 ? <Empty title="No models registered" /> : <ModelsTable rows={rows} />)}
          </Async>
        </section>
      </div>
    </div>
  );
}
