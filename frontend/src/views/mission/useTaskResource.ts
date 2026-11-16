import { useEffect, useState } from 'react';
import { useResource, type Resource } from '../../lib/useResource';
import { useStore } from '../../store/store';

/** Refetches when the store records a non-streaming event for this task (debounced). */
export function useTaskResource<T>(taskId: string, fetcher: () => Promise<T>): Resource<T> {
  const rev = useStore((s) => s.data.taskRev[taskId] ?? 0);
  const [settled, setSettled] = useState(rev);
  useEffect(() => {
    const t = setTimeout(() => setSettled(rev), 400);
    return () => clearTimeout(t);
  }, [rev]);
  return useResource(fetcher, [taskId, settled]);
}
