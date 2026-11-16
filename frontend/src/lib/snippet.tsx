import type { ReactNode } from 'react';

export interface SnippetPart {
  text: string;
  mark: boolean;
}

const OPEN = '«';
const CLOSE = '»';

/**
 * Splits a knowledge snippet on the backend's « » match markers. The result is plain text segments only:
 * the caller renders them as React text nodes (never as HTML), so a hostile snippet cannot inject markup.
 * Unbalanced markers are shown literally rather than guessed at.
 */
export function splitSnippet(snippet: string): SnippetPart[] {
  const parts: SnippetPart[] = [];
  let i = 0;
  const push = (text: string, mark: boolean) => {
    if (text) parts.push({ text, mark });
  };
  while (i < snippet.length) {
    const open = snippet.indexOf(OPEN, i);
    if (open < 0) break;
    const close = snippet.indexOf(CLOSE, open + 1);
    if (close < 0) break;
    const inner = snippet.slice(open + 1, close);
    if (inner.includes(OPEN)) {
      // a stray opener before the real one: emit up to it literally and retry from there
      push(snippet.slice(i, open + 1), false);
      i = open + 1;
      continue;
    }
    push(snippet.slice(i, open), false);
    push(inner, true);
    i = close + 1;
  }
  push(snippet.slice(i), false);
  return parts;
}

export function Snippet({ text }: { text: string }): ReactNode {
  return (
    <>
      {splitSnippet(text).map((p, i) =>
        p.mark ? (
          <mark key={i} className="bg-amber/25 px-0.5 text-ink">
            {p.text}
          </mark>
        ) : (
          <span key={i}>{p.text}</span>
        ),
      )}
    </>
  );
}
