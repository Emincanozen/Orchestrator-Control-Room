import { ArrowDown, ArrowUp, RotateCcw, Save } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../../api/endpoints';
import type { PreferenceField, PreferencesUpdate, ProviderModel, RoutingPreferences } from '../../api/types';
import { Async, ErrorBox } from '../../components/ui';
import { useResource } from '../../lib/useResource';

const same = (a: string[], b: string[]) => a.length === b.length && a.every((x, i) => x === b[i]);
const sameSet = (a: string[], b: string[]) => a.length === b.length && a.every((x) => b.includes(x));

function SourceChip({ overridden }: { overridden: boolean }) {
  return overridden ? (
    <span className="chip text-amber" title="Set by you; routing.yaml is not used for this value">
      overridden
    </span>
  ) : (
    <span className="chip text-faint" title="Value from config/routing.yaml">
      default
    </span>
  );
}

function SectionShell({
  title,
  hint,
  field,
  prefs,
  dirty,
  busy,
  onSave,
  onReset,
  children,
}: {
  title: string;
  hint: string;
  field: PreferenceField;
  prefs: RoutingPreferences;
  dirty: boolean;
  busy: boolean;
  onSave: () => void;
  onReset: () => void;
  children: React.ReactNode;
}) {
  const overridden = prefs.overridden.includes(field);
  const def = prefs.defaults[field];
  return (
    <section className="pixel-card flex flex-col gap-2 p-3" aria-label={title} data-field={field} data-overridden={overridden}>
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="display text-[13px]">{title}</h3>
        <SourceChip overridden={overridden} />
        <span className="ml-auto flex gap-2">
          <button className="btn btn-sm" onClick={onReset} disabled={busy || !overridden} title="Remove the override and use the default from routing.yaml" aria-label={`Reset ${title} to default`}>
            <RotateCcw className="size-3" aria-hidden /> Reset to default
          </button>
          <button className="btn btn-sm btn-primary" onClick={onSave} disabled={busy || !dirty} aria-label={`Save ${title}`}>
            <Save className="size-3" aria-hidden /> Save
          </button>
        </span>
      </div>
      <p className="text-xs text-muted">{hint}</p>
      {children}
      <p className="text-[11px] text-faint">Default: {def.length ? def.join(', ') : 'none'}</p>
    </section>
  );
}

function ProviderOrder({ prefs, providers, run, busy }: { prefs: RoutingPreferences; providers: string[]; run: (u: PreferencesUpdate) => void; busy: boolean }) {
  // Show the effective order first, then any known provider that is not in it (it ranks after the listed ones).
  const providersKey = providers.join('|');
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const initial = useMemo(() => [...prefs.provider_order, ...providers.filter((p) => !prefs.provider_order.includes(p))], [prefs.provider_order, providersKey]);
  const [order, setOrder] = useState(initial);
  const focusId = useRef<string | null>(null);
  useEffect(() => setOrder(initial), [initial]);
  useEffect(() => {
    if (!focusId.current) return;
    const el = document.getElementById(focusId.current) as HTMLButtonElement | null;
    if (el && !el.disabled) el.focus();
    else document.getElementById(focusId.current.replace(/-(up|down)$/, (_, d) => (d === 'up' ? '-down' : '-up')))?.focus();
    focusId.current = null;
  });
  const move = (i: number, delta: -1 | 1) => {
    const j = i + delta;
    if (j < 0 || j >= order.length) return;
    const next = [...order];
    [next[i], next[j]] = [next[j], next[i]];
    focusId.current = `mv-${order[i]}-${delta < 0 ? 'up' : 'down'}`;
    setOrder(next);
  };
  const listed = new Set(prefs.provider_order);
  return (
    <SectionShell
      title="Provider order"
      hint="Earlier providers score higher on the preference component. It never overrides a hard filter (quota, capability, tier)."
      field="provider_order"
      prefs={prefs}
      dirty={!same(order, initial)}
      busy={busy}
      onSave={() => run({ provider_order: order })}
      onReset={() => run({ provider_order: null })}
    >
      <ol className="flex flex-col gap-1" aria-label="Provider order">
        {order.map((name, i) => (
          <li key={name} className="flex items-center gap-2 border border-line bg-sunken px-2 py-1 text-xs">
            <span className="display w-5 text-muted">{i + 1}</span>
            <span className="font-mono">{name}</span>
            {!listed.has(name) && <span className="text-[11px] text-faint">not in the saved order</span>}
            <span className="ml-auto flex gap-1">
              <button id={`mv-${name}-up`} className="btn btn-sm btn-ghost" aria-label={`Move ${name} up`} disabled={busy || i === 0} onClick={() => move(i, -1)}>
                <ArrowUp className="size-3.5" aria-hidden />
              </button>
              <button id={`mv-${name}-down`} className="btn btn-sm btn-ghost" aria-label={`Move ${name} down`} disabled={busy || i === order.length - 1} onClick={() => move(i, 1)}>
                <ArrowDown className="size-3.5" aria-hidden />
              </button>
            </span>
          </li>
        ))}
        {order.length === 0 && <li className="text-xs text-faint">No providers known.</li>}
      </ol>
    </SectionShell>
  );
}

function DisabledProviders({ prefs, providers, run, busy }: { prefs: RoutingPreferences; providers: string[]; run: (u: PreferencesUpdate) => void; busy: boolean }) {
  const [sel, setSel] = useState(prefs.disabled_providers);
  useEffect(() => setSel(prefs.disabled_providers), [prefs.disabled_providers]);
  const all = [...new Set([...providers, ...prefs.disabled_providers])].sort();
  const toggle = (p: string) => setSel((s) => (s.includes(p) ? s.filter((x) => x !== p) : [...s, p]));
  return (
    <SectionShell
      title="Disabled providers"
      hint="A disabled provider is never routed to, whatever its score."
      field="disabled_providers"
      prefs={prefs}
      dirty={!sameSet(sel, prefs.disabled_providers)}
      busy={busy}
      onSave={() => run({ disabled_providers: sel })}
      onReset={() => run({ disabled_providers: null })}
    >
      <ul className="flex flex-wrap gap-3">
        {all.map((p) => (
          <li key={p}>
            <label className="flex items-center gap-1.5 text-xs">
              <input type="checkbox" checked={sel.includes(p)} onChange={() => toggle(p)} /> Disable {p}
            </label>
          </li>
        ))}
        {all.length === 0 && <li className="text-xs text-faint">No providers known.</li>}
      </ul>
    </SectionShell>
  );
}

function DisabledModels({ prefs, models, run, busy }: { prefs: RoutingPreferences; models: ProviderModel[]; run: (u: PreferencesUpdate) => void; busy: boolean }) {
  const [sel, setSel] = useState(prefs.disabled_models);
  useEffect(() => setSel(prefs.disabled_models), [prefs.disabled_models]);
  const isOn = (m: ProviderModel) => sel.includes(m.model_id) || sel.includes(`${m.provider}/${m.model_id}`);
  const toggle = (m: ProviderModel) =>
    setSel((s) => (isOn(m) ? s.filter((x) => x !== m.model_id && x !== `${m.provider}/${m.model_id}`) : [...s, `${m.provider}/${m.model_id}`]));
  const known = new Set(models.flatMap((m) => [m.model_id, `${m.provider}/${m.model_id}`]));
  const stray = sel.filter((s) => !known.has(s));
  return (
    <SectionShell
      title="Disabled models"
      hint="A disabled model is skipped even when its provider is enabled."
      field="disabled_models"
      prefs={prefs}
      dirty={!sameSet(sel, prefs.disabled_models)}
      busy={busy}
      onSave={() => run({ disabled_models: sel })}
      onReset={() => run({ disabled_models: null })}
    >
      <ul className="grid grid-cols-[repeat(auto-fill,minmax(16rem,1fr))] gap-x-3 gap-y-1">
        {models.map((m) => (
          <li key={`${m.provider}/${m.model_id}`}>
            <label className="flex items-center gap-1.5 text-xs">
              <input type="checkbox" checked={isOn(m)} onChange={() => toggle(m)} />
              <span>
                Disable <span className="font-mono">{m.provider}/{m.model_id}</span>
              </span>
            </label>
          </li>
        ))}
        {stray.map((s) => (
          <li key={s} className="flex items-center gap-2 text-xs text-muted">
            <span className="font-mono">{s}</span> (not in the registry)
            <button className="btn btn-sm btn-ghost" onClick={() => setSel((x) => x.filter((y) => y !== s))} aria-label={`Remove ${s}`}>
              remove
            </button>
          </li>
        ))}
      </ul>
    </SectionShell>
  );
}

export function PreferencesEditor({ providerNames, rev }: { providerNames: string[]; rev: number }) {
  const prefs = useResource(() => api.routingPreferences(), [rev]);
  const models = useResource(() => api.models(), []);
  const [data, setData] = useState<RoutingPreferences | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    if (prefs.data) setData(prefs.data);
  }, [prefs.data]);

  const run = async (u: PreferencesUpdate) => {
    setBusy(true);
    setError(null);
    try {
      setData(await api.putRoutingPreferences(u));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section aria-label="Routing preferences" className="flex flex-col gap-3">
      <h2 className="label">Preferences</h2>
      {error ? <ErrorBox error={error} /> : null}
      <Async res={prefs} what="Routing preferences">
        {() =>
          data && (
            <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
              <ProviderOrder prefs={data} providers={providerNames} run={run} busy={busy} />
              <DisabledProviders prefs={data} providers={providerNames} run={run} busy={busy} />
              <div className="xl:col-span-2">
                <DisabledModels prefs={data} models={models.data ?? []} run={run} busy={busy} />
              </div>
            </div>
          )
        }
      </Async>
    </section>
  );
}
