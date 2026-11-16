import { useCallback, useEffect, useRef } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api } from '../api/endpoints';
import { ViewHeader } from '../components/ui';
import { useResource } from '../lib/useResource';
import { useStore } from '../store/store';
import { DecisionsList, PreviewForm } from './routing/Decisions';
import { PreferencesEditor } from './routing/Preferences';

export function RoutingView() {
  const [params, setParams] = useSearchParams();
  const taskId = params.get('task') ?? '';
  const rev = useStore((s) => s.data.rev);
  const providers = useResource(() => api.providers(), [rev.providers]);
  const names = (providers.data ?? []).map((p) => p.provider);
  const decisionsRef = useRef<HTMLDivElement>(null);
  const pendingScroll = useRef(false);

  // Arriving with ?task= (a task's "Why this model?" link): bring that task's decisions into view once, after
  // the content above them has loaded, instead of leaving them below the preferences and preview form.
  // Later live updates never move the page again.
  useEffect(() => {
    pendingScroll.current = !!taskId;
  }, [taskId]);
  const onDecisionsLoaded = useCallback(() => {
    if (!pendingScroll.current || !providers.data) return;
    pendingScroll.current = false;
    requestAnimationFrame(() => decisionsRef.current?.scrollIntoView?.({ block: 'start' }));
  }, [providers.data]);

  return (
    <div>
      <ViewHeader title="Routing" subtitle="How the router picks a model: your preferences, a side-effect-free preview, and an explanation for every decision" />
      <div className="flex flex-col gap-8 px-5 pb-8">
        <PreferencesEditor providerNames={names} rev={rev.providers} />
        <PreviewForm />
        <div ref={decisionsRef} className="scroll-mt-4">
          <DecisionsList
            taskId={taskId}
            rev={rev.routing}
            onLoaded={onDecisionsLoaded}
            onTaskChange={(id) => setParams(id ? { task: id } : {}, { replace: true })}
          />
        </div>
      </div>
    </div>
  );
}
