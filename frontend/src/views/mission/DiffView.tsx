export type DiffLineKind = 'file' | 'hunk' | 'add' | 'del' | 'ctx' | 'meta';

export function classifyDiffLine(line: string): DiffLineKind {
  if (line.startsWith('diff --git') || line.startsWith('+++') || line.startsWith('---')) return 'file';
  if (line.startsWith('@@')) return 'hunk';
  if (line.startsWith('+')) return 'add';
  if (line.startsWith('-')) return 'del';
  if (line.startsWith('index ') || line.startsWith('new file') || line.startsWith('deleted file') || line.startsWith('similarity') || line.startsWith('rename')) return 'meta';
  return 'ctx';
}

const STYLE: Record<DiffLineKind, string> = {
  file: 'text-ink font-semibold bg-raised',
  hunk: 'text-signal bg-signal/10',
  add: 'text-ok bg-ok/10',
  del: 'text-danger bg-danger/10',
  ctx: 'text-muted',
  meta: 'text-faint',
};

/** Renders a unified diff as text (never as HTML). */
export function DiffView({ diff }: { diff: string }) {
  const lines = diff.split('\n');
  return (
    <pre className="m-0 overflow-auto p-0 font-mono text-xs leading-5" aria-label="Unified diff" data-testid="diff">
      {lines.map((l, i) => {
        const kind = classifyDiffLine(l);
        return (
          <div key={i} className={`whitespace-pre px-3 ${STYLE[kind]}`} data-kind={kind}>
            {l || ' '}
          </div>
        );
      })}
    </pre>
  );
}
