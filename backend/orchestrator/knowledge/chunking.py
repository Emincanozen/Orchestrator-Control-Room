"""Heading-based chunking for markdown / plain text with size bounds."""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_CHUNK_CHARS = 2000
MIN_CHUNK_CHARS = 200
MAX_CHUNKS_PER_FILE = 60

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True)
class Chunk:
    heading: str  # breadcrumb, e.g. "Architecture > Storage"
    text: str


def _split_long(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    cur = ""
    for para in re.split(r"\n\s*\n", text):
        while len(para) > limit:  # a single huge paragraph: hard split on line/space boundaries
            cut = para.rfind("\n", 0, limit)
            if cut < limit // 2:
                cut = para.rfind(" ", 0, limit)
            if cut < limit // 2:
                cut = limit
            if cur:
                out.append(cur)
                cur = ""
            out.append(para[:cut])
            para = para[cut:].lstrip()
        if cur and len(cur) + len(para) + 2 > limit:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        out.append(cur)
    return [p for p in out if p.strip()]


def chunk_markdown(text: str, *, fallback_title: str = "", max_chars: int = MAX_CHUNK_CHARS,
                   min_chars: int = MIN_CHUNK_CHARS, max_chunks: int = MAX_CHUNKS_PER_FILE) -> list[Chunk]:
    """Split on markdown headings (outside code fences); tiny sections merge into the next one,
    oversized ones split on paragraphs. Never returns empty chunks."""
    sections: list[tuple[str, list[str]]] = []
    stack: list[tuple[int, str]] = []
    cur_head = fallback_title
    cur_lines: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        m = None if in_fence else _HEADING.match(line)
        if m:
            sections.append((cur_head, cur_lines))
            level, title = len(m.group(1)), m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            cur_head = " > ".join(t for _, t in stack)
            cur_lines = [line]
        else:
            cur_lines.append(line)
    sections.append((cur_head, cur_lines))

    chunks: list[Chunk] = []
    pending_head, pending = "", ""
    for head, lines in sections:
        body = "\n".join(lines).strip()
        if not body:
            continue
        if pending:
            body = f"{pending}\n\n{body}"
            head = f"{pending_head} > {head}" if pending_head and head and pending_head != head else (pending_head or head)
            pending = ""
        if len(body) < min_chars:
            pending_head, pending = head, body
            continue
        for part in _split_long(body, max_chars):
            chunks.append(Chunk(head, part))
    if pending:
        if chunks and len(pending) < min_chars and len(chunks[-1].text) + len(pending) < max_chars:
            last = chunks.pop()
            chunks.append(Chunk(last.heading, f"{last.text}\n\n{pending}"))
        else:
            chunks.append(Chunk(pending_head, pending))
    return chunks[:max_chunks]
