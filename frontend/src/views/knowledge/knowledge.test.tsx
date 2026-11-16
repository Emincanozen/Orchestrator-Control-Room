import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../api/endpoints', () => ({
  api: {
    knowledgeSearch: vi.fn(),
    knowledgeIndex: vi.fn(),
    knowledgeJob: vi.fn(),
    knowledgeDocs: vi.fn(),
    knowledgeDoc: vi.fn(),
    deleteKnowledgeDoc: vi.fn(),
    knowledgeStats: vi.fn(),
  },
}));
import { api } from '../../api/endpoints';
import { splitSnippet } from '../../lib/snippet';
import { initialData } from '../../store/reducer';
import { useStore } from '../../store/store';
import { mkProject } from '../../test/fixtures';
import { KnowledgeView } from '../KnowledgeView';

const m = vi.mocked(api, true);

const stats = {
  docs: 2,
  by_kind: { doc: 2 },
  by_project: { prj_1: 2 },
  chars: 900,
  last_updated: '2026-11-11T10:00:00Z',
  vectors: 0,
  vector_search: { enabled: false, kinds: [], embedder: 'hash', semantic: false },
};
const page = (n = 2) => ({
  total: n,
  limit: 25,
  offset: 0,
  docs: Array.from({ length: n }, (_, i) => ({ id: `kdoc_${i}`, project_id: 'prj_1', kind: 'doc', title: `Doc ${i}`, source_path: `docs/d${i}.md`, source_hash: 'abcdef0123456789abcd', updated_at: '2026-11-11T10:00:00Z', chars: 100 })),
});

function renderView() {
  return render(
    <MemoryRouter>
      <KnowledgeView />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  useStore.setState({ data: { ...initialData(), projects: { prj_1: mkProject() } } });
  m.knowledgeStats.mockResolvedValue(stats);
  m.knowledgeDocs.mockResolvedValue(page());
});

describe('snippet markers', () => {
  it('splits marker pairs into marked and plain parts', () => {
    expect(splitSnippet('a «b» c «d»')).toEqual([
      { text: 'a ', mark: false },
      { text: 'b', mark: true },
      { text: ' c ', mark: false },
      { text: 'd', mark: true },
    ]);
  });
  it('keeps unbalanced markers literally', () => {
    expect(splitSnippet('x » y « z')).toEqual([{ text: 'x » y « z', mark: false }]);
  });
});

describe('search', () => {
  it('renders matches as <mark> and never interprets hostile HTML', async () => {
    const hostile = 'before <img src=x onerror="window.__pwned=1"> «<script>window.__pwned=2</script>» after <b>bold</b>';
    m.knowledgeSearch.mockResolvedValue({
      results: [{ doc_id: 'kdoc_0', kind: 'doc', title: '<i>Hostile title</i>', source_path: 'docs/x.md', project_id: 'prj_1', updated_at: '2026-11-11T10:00:00Z', score: 1.234, snippet: hostile }],
      used: { fts: true, vectors: false },
    });
    const { container } = renderView();
    await userEvent.type(await screen.findByRole('textbox', { name: 'Search knowledge' }), 'hostile');
    await userEvent.click(screen.getByRole('button', { name: /^Search$/ }));
    const snip = await screen.findByTestId('snippet');
    expect(snip.querySelectorAll('mark')).toHaveLength(1);
    expect(snip.querySelector('mark')?.textContent).toBe('<script>window.__pwned=2</script>');
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('b')).toBeNull();
    expect(container.querySelector('i')).toBeNull();
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
    expect(snip.textContent).toContain('<img src=x onerror="window.__pwned=1">');
    expect(screen.getByText('<i>Hostile title</i>')).toBeInTheDocument();
  });

  it('shows provenance and whether vectors were used, and states lexical search', async () => {
    m.knowledgeSearch.mockResolvedValue({
      results: [{ doc_id: 'kdoc_0', kind: 'adr', title: 'ADR 1', source_path: 'docs/adr/1.md', project_id: 'prj_1', updated_at: '2026-11-11T10:00:00Z', score: 2.5, snippet: 'use «sqlite»' }],
      used: { fts: true, vectors: true },
    });
    renderView();
    expect(await screen.findByTestId('lexical-note')).toHaveTextContent(/full-text \(lexical/);
    expect(screen.getByTestId('lexical-note').textContent).toMatch(/not semantic/);
    await userEvent.type(screen.getByRole('textbox', { name: 'Search knowledge' }), 'sqlite');
    await userEvent.click(screen.getByRole('button', { name: /^Search$/ }));
    const prov = await screen.findByTestId('provenance');
    expect(prov).toHaveTextContent('docs/adr/1.md');
    expect(prov).toHaveTextContent('project: Demo');
    expect(prov).toHaveTextContent('score 2.500');
    expect(screen.getByTestId('used-vectors')).toHaveTextContent('hash vectors used (not semantic)');
    expect(m.knowledgeSearch).toHaveBeenCalledWith({ query: 'sqlite', project_id: null, kinds: undefined, limit: 20 });
  });

  it('passes project and kind filters', async () => {
    m.knowledgeSearch.mockResolvedValue({ results: [], used: { fts: true, vectors: false } });
    renderView();
    await userEvent.type(await screen.findByRole('textbox', { name: 'Search knowledge' }), 'q');
    await userEvent.selectOptions(document.getElementById('search-project') as HTMLSelectElement, 'prj_1');
    await userEvent.click(screen.getByRole('checkbox', { name: 'adr' }));
    await userEvent.click(screen.getByRole('button', { name: /^Search$/ }));
    await waitFor(() => expect(m.knowledgeSearch).toHaveBeenCalledWith({ query: 'q', project_id: 'prj_1', kinds: ['adr'], limit: 20 }));
    expect(await screen.findByText('No matches')).toBeInTheDocument();
  });
});

describe('documents', () => {
  it('opens a document as plain text and deletes it behind a confirm', async () => {
    m.knowledgeDoc.mockResolvedValue({ id: 'kdoc_0', project_id: 'prj_1', kind: 'doc', title: 'Doc 0', source_path: 'docs/d0.md', source_hash: 'abcdef0123456789abcd', updated_at: '2026-11-11T10:00:00Z', body: '# Title <b>raw</b>' });
    m.deleteKnowledgeDoc.mockResolvedValue(undefined);
    renderView();
    await userEvent.click(await screen.findByRole('button', { name: 'Doc 0' }));
    expect(await screen.findByTestId('doc-body')).toHaveTextContent('# Title <b>raw</b>');
    expect(document.querySelector('[data-testid="doc-body"] b')).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'Delete from index' }));
    expect(m.deleteKnowledgeDoc).not.toHaveBeenCalled();
    const dlgs = await screen.findAllByRole('dialog');
    await userEvent.click(within(dlgs[dlgs.length - 1]).getByRole('button', { name: 'Delete' }));
    expect(m.deleteKnowledgeDoc).toHaveBeenCalledWith('kdoc_0');
  });

  it('pages the browser', async () => {
    m.knowledgeDocs.mockResolvedValue({ ...page(25), total: 40 });
    renderView();
    expect(await screen.findByTestId('page-info')).toHaveTextContent('1-25 of 40');
    await userEvent.click(screen.getByRole('button', { name: 'Next' }));
    await waitFor(() => expect(m.knowledgeDocs).toHaveBeenLastCalledWith({ project_id: undefined, kind: undefined, limit: 25, offset: 25 }));
  });
});

describe('index now', () => {
  it('starts a job, polls it and shows the report with skipped reasons and errors', async () => {
    m.knowledgeIndex.mockResolvedValue({ job_id: 'job_1', status: 'running' });
    m.knowledgeJob
      .mockResolvedValueOnce({ job_id: 'job_1', status: 'running', report: null })
      .mockResolvedValue({
        job_id: 'job_1',
        status: 'done',
        report: { added: 3, updated: 1, removed: 0, skipped: 1, errors: ['docs:x.md: boom'], skipped_details: [{ path: '.env', reason: 'secret file' }] },
      });
    renderView();
    await userEvent.click(await screen.findByRole('button', { name: /Index now/ }));
    const dlg = await screen.findByRole('dialog');
    await userEvent.click(within(dlg).getByRole('checkbox', { name: /Full reindex/ }));
    await userEvent.click(within(dlg).getByRole('button', { name: 'Start indexing' }));
    expect(m.knowledgeIndex).toHaveBeenCalledWith({ project_id: null, sources: undefined, full: true });
    const report = await within(dlg).findByTestId('index-report', undefined, { timeout: 4000 });
    expect(within(report).getByTestId('report-added')).toHaveTextContent('3');
    expect(report).toHaveTextContent('secret file');
    expect(report).toHaveTextContent('boom');
  });
});
