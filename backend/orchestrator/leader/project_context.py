"""Bounded project context for leader prompts: a file-tree SUMMARY, never file contents or full dumps."""

from __future__ import annotations

import os
from pathlib import Path

SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea", ".vscode", "target", ".next"})
MAX_ENTRIES = 80
MAX_DEPTH = 2
MAX_CHARS = 3_000


def tree_summary(root: str, *, max_entries: int = MAX_ENTRIES, max_depth: int = MAX_DEPTH, max_chars: int = MAX_CHARS) -> str:
    """Top of the directory tree (depth <= max_depth, at most `max_entries` lines, `max_chars` characters).
    Directories show how many direct entries they hold; deeper content is only counted."""
    base = Path(root)
    if not base.is_dir():
        return "(project directory not available)"
    lines: list[str] = []
    total = 0
    truncated = False

    def walk(path: Path, depth: int) -> None:
        nonlocal total, truncated
        try:
            entries = sorted(os.scandir(path), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
        except OSError:
            return
        for e in entries:
            if e.name in SKIP_DIRS or e.name.startswith(".") and e.name not in {".github"}:
                continue
            total += 1
            if len(lines) >= max_entries:
                truncated = True
                continue
            indent = "  " * depth
            if e.is_dir(follow_symlinks=False):
                try:
                    n = sum(1 for _ in os.scandir(e.path))
                except OSError:
                    n = 0
                lines.append(f"{indent}{e.name}/ ({n} entries)")
                if depth + 1 < max_depth:
                    walk(Path(e.path), depth + 1)
            else:
                lines.append(f"{indent}{e.name}")

    walk(base, 0)
    out = "\n".join(lines)
    if truncated:
        out += f"\n... ({total - len(lines)} more entries not shown)"
    if len(out) > max_chars:
        out = out[:max_chars] + "\n...(truncated)"
    return out or "(empty project)"
