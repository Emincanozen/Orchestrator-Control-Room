import { Database, RefreshCw, Search } from 'lucide-react';
import { useState, type FormEvent } from 'react';
import { api } from '../api/endpoints';
import { isUnavailable } from '../api/client';
import { KNOWLEDGE_KINDS, type KnowledgeSearchResponse } from '../api/types';
import { Async, Empty, ErrorBox, Section, Unavailable, ViewHeader } from '../components/ui';
import { Snippet } from '../lib/snippet';
import { formatDateTime } from '../lib/time';
import { useResource } from '../lib/useResource';
import { useStore } from '../store/store';
import { DocViewer, IndexDialog, LEXICAL_NOTE, ProjectSelect, StatsPanel } from './knowledge/parts';

const PAGE = 25;

export function KnowledgeView() {
  const rev = useStore((s) => s.data.rev.knowledge);
  const projects = useStore((s) => s.data.projects);
  const stats = useResource(() => api.knowledgeStats(), [rev]);

  const [q, setQ] = useState('');
  const [searchProject, setSearchProject] = useState('');
  const [kinds, setKinds] = useState<string[]>([]);
  const [res, setRes] = useState<KnowledgeSearchResponse | null>(null);
  const [searched, setSearched] = useState('');
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const [browseProject, setBrowseProject] = useState('');
  const [browseKind, setBrowseKind] = useState('');
  const [offset, setOffset] = useState(0);
  const docs = useResource(
    () => api.knowledgeDocs({ project_id: browseProject || undefined, kind: browseKind || undefined, limit: PAGE, offset }),
    [rev, browseProject, browseKind, offset],
  );

  const [viewing, setViewing] = useState<string | null>(null);
  const [indexing, setIndexing] = useState(false);

  const search = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setSearching(true);
    try {
      setRes(await api.knowledgeSearch({ query: q.trim(), project_id: searchProject || null, kinds: kinds.length ? kinds : undefined, limit: 20 }));
      setSearched(q.trim());
    } catch (err) {
      setRes(null);
      setError(err);
    } finally {
      setSearching(false);
    }
  };
  const toggleKind = (k: string) => setKinds((cur) => (cur.includes(k) ? cur.filter((x) => x !== k) : [...cur, k]));
  const reloadAll = () => {
    docs.reload();
    stats.reload();
  };

  return (
    <div>
      <ViewHeader
        title="Knowledge"
        subtitle="Full-text (lexical) search over indexed requirements, ADRs, docs and task history. Explicit search only."
        actions={
          <>
            <button className="btn btn-sm" onClick={reloadAll} aria-label="Refresh knowledge">
              <RefreshCw className="size-3.5" aria-hidden /> Refresh
            </button>
            <button className="btn btn-primary" onClick={() => setIndexing(true)}>
              <Database className="size-3.5" aria-hidden /> Index now
            </button>
          </>
        }
      />
      <div className="flex flex-col gap-4 px-5 pb-8">
        {isUnavailable(stats.error) ? (
          <Unavailable what="Knowledge" />
        ) : (
          <>
            <Async res={stats} what="Knowledge statistics">
              {(s) => <StatsPanel stats={s} />}
            </Async>
            <Section title="Search" aria="Search knowledge">
              <p className="text-[11px] text-faint" data-testid="lexical-note">
                {LEXICAL_NOTE}
              </p>
              <form onSubmit={search} className="flex flex-col gap-3" role="search">
                <div className="flex flex-wrap gap-2">
                  <input className="field max-w-xl flex-1" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search words in the indexed knowledge..." aria-label="Search knowledge" />
                  <ProjectSelect id="search-project" value={searchProject} onChange={setSearchProject} allLabel="All projects" />
                  <button className="btn btn-primary" disabled={!q.trim() || searching}>
                    <Search className="size-3.5" aria-hidden /> Search
                  </button>
                </div>
                <fieldset className="flex flex-wrap items-center gap-x-3 gap-y-1">
                  <legend className="sr-only">Kinds</legend>
                  <span className="label">Kinds</span>
                  {KNOWLEDGE_KINDS.map((k) => (
                    <label key={k} className="flex items-center gap-1 text-xs text-muted">
                      <input type="checkbox" checked={kinds.includes(k)} onChange={() => toggleKind(k)} />
                      {k}
                    </label>
                  ))}
                  <span className="text-[11px] text-faint">{kinds.length === 0 ? '(all kinds)' : ''}</span>
                </fieldset>
              </form>
              {error ? <ErrorBox error={error} /> : null}
              {res && (
                <div aria-label="Search results" data-testid="search-results">
                  <div className="mb-2 flex flex-wrap items-center gap-2 text-xs text-muted">
                    <span>
                      {res.results.length} result{res.results.length === 1 ? '' : 's'} for &quot;{searched}&quot;
                    </span>
                    <span className="chip text-signal" data-testid="used-fts">
                      full-text
                    </span>
                    <span className={`chip ${res.used.vectors ? 'text-amber' : 'text-faint'}`} data-testid="used-vectors">
                      {res.used.vectors ? 'hash vectors used (not semantic)' : 'vectors not used'}
                    </span>
                  </div>
                  {res.results.length === 0 ? (
                    <Empty title="No matches" hint="Search matches whole words; try fewer or different words, or run Index now." />
                  ) : (
                    <ul className="flex flex-col gap-2">
                      {res.results.map((h) => (
                        <li key={h.doc_id} className="border border-line bg-sunken p-3 text-[13px]" data-testid="hit">
                          <div className="flex flex-wrap items-center gap-2">
                            <button className="text-left font-medium text-signal underline" onClick={() => setViewing(h.doc_id)}>
                              {h.title}
                            </button>
                            <span className="chip text-signal">{h.kind}</span>
                          </div>
                          <p className="mt-1 text-xs text-muted" data-testid="snippet">
                            <Snippet text={h.snippet} />
                          </p>
                          <dl className="mt-1 flex flex-wrap gap-x-4 text-[11px] text-faint" data-testid="provenance">
                            <span className="font-mono">{h.source_path ?? 'no source path'}</span>
                            <span>project: {h.project_id ? (projects[h.project_id]?.name ?? h.project_id) : 'global'}</span>
                            <span>updated {formatDateTime(h.updated_at)}</span>
                            <span>score {h.score.toFixed(3)}</span>
                          </dl>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
            </Section>
            <Section
              title="Indexed documents"
              aria="Indexed documents"
              actions={
                <div className="flex items-center gap-2">
                  <ProjectSelect id="browse-project" value={browseProject} onChange={(v) => { setBrowseProject(v); setOffset(0); }} allLabel="All projects" />
                  <select className="field w-auto" aria-label="Kind" value={browseKind} onChange={(e) => { setBrowseKind(e.target.value); setOffset(0); }}>
                    <option value="">All kinds</option>
                    {KNOWLEDGE_KINDS.map((k) => (
                      <option key={k} value={k}>
                        {k}
                      </option>
                    ))}
                  </select>
                </div>
              }
            >
              <Async res={docs} what="Knowledge documents">
                {(p) =>
                  p.docs.length === 0 ? (
                    <Empty title="Nothing indexed yet" hint="Use Index now to build the local index." />
                  ) : (
                    <>
                      <table className="w-full text-[13px]" aria-label="Documents">
                        <thead className="text-left">
                          <tr className="label">
                            <th className="py-1 pr-3 font-normal">Title</th>
                            <th className="pr-3 font-normal">Kind</th>
                            <th className="pr-3 font-normal">Source</th>
                            <th className="pr-3 text-right font-normal">Chars</th>
                            <th className="font-normal">Updated</th>
                          </tr>
                        </thead>
                        <tbody>
                          {p.docs.map((d) => (
                            <tr key={d.id} className="border-t border-line/60" data-testid="doc-row">
                              <td className="max-w-80 truncate py-1 pr-3">
                                <button className="text-signal underline" onClick={() => setViewing(d.id)}>
                                  {d.title}
                                </button>
                              </td>
                              <td className="pr-3">
                                <span className="chip text-signal">{d.kind}</span>
                              </td>
                              <td className="max-w-72 truncate pr-3 font-mono text-xs text-faint">{d.source_path}</td>
                              <td className="tick-num pr-3 text-right">{d.chars}</td>
                              <td className="text-xs text-faint">{formatDateTime(d.updated_at)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                      <div className="flex items-center gap-3 text-xs text-muted">
                        <button className="btn btn-sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
                          Previous
                        </button>
                        <span data-testid="page-info">
                          {p.offset + 1}-{p.offset + p.docs.length} of {p.total}
                        </span>
                        <button className="btn btn-sm" disabled={p.offset + p.docs.length >= p.total} onClick={() => setOffset(offset + PAGE)}>
                          Next
                        </button>
                      </div>
                    </>
                  )
                }
              </Async>
            </Section>
          </>
        )}
      </div>
      {viewing && (
        <DocViewer
          docId={viewing}
          onClose={() => setViewing(null)}
          onDeleted={() => {
            setViewing(null);
            setRes((r) => (r ? { ...r, results: r.results.filter((h) => h.doc_id !== viewing) } : r));
            reloadAll();
          }}
        />
      )}
      {indexing && <IndexDialog onClose={() => { setIndexing(false); reloadAll(); }} />}
    </div>
  );
}
