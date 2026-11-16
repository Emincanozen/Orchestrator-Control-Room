"""Agent instruction and configuration files in a task worktree (N2 re-review S6).

A sandboxed verification command may write the worktree. Some files there are not just code: the next provider
run (Medium integrity, the user's own token) LOADS them - as instructions (prompt injection), as configuration
that starts programs (MCP servers, hooks), or as permission settings. The sandbox snapshots this set before every
verification command; anything the command changed, created or deleted is restored and reported as tampering,
which blocks the task (`sandbox.Session`).

The set lives here, in one place. Matching is case-insensitive (NTFS) and ignores trailing dots/spaces.
"""

from __future__ import annotations

import hashlib
import os
import stat as stat_mod
from dataclasses import dataclass

from orchestrator.security import paths as path_policy

# Instruction files, at ANY depth: Claude Code loads CLAUDE.md files of subdirectories it works in; Codex and
# Antigravity read AGENTS.md / GEMINI.md along the path from the repository root to the working directory.
INSTRUCTION_FILES = frozenset({
    "claude.md",          # Claude Code project memory / instructions
    "claude.local.md",    # Claude Code local (unshared) instructions
    "agents.md",          # Codex (and Antigravity) instructions
    "agents.override.md",  # Codex: replaces AGENTS.md at the same level
    "gemini.md",          # Antigravity / Gemini instructions
})

# Configuration at the worktree ROOT that a provider CLI reads when it starts in the worktree.
ROOT_FILES = frozenset({
    ".mcp.json",          # Claude Code project MCP servers: each entry is a program started at Medium
})
ROOT_DIRS = frozenset({
    ".claude",            # Claude Code settings.json (hooks = programs, permissions), agents, commands, skills
    ".codex",             # Codex project config.toml (MCP servers, sandbox / approval settings), prompts
    ".agents",            # Codex / agent skills directory
    ".agent",             # Antigravity workspace rules and workflows
    ".gemini",            # Gemini / Antigravity settings (MCP servers, tools)
    ".antigravity",       # Antigravity workspace settings (defensive: exact layout not verified)
    ".vscode",            # VS Code-based editors (Antigravity): tasks.json can auto-run tasks on folder open
})

MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024  # content kept for restoration (all files together)
MAX_ENTRIES = 2_000_000  # walk bound


def _norm(name: str) -> str:
    return name.rstrip(" .").casefold()


@dataclass(frozen=True)
class Entry:
    kind: str  # "file" | "dir" | "link" | "other"
    digest: str = ""
    size: int = 0


class Snapshot:
    def __init__(self, entries: dict[str, Entry], contents: dict[str, bytes]):
        self.entries = entries
        self.contents = contents


def _is_reparse(st: os.stat_result) -> bool:
    return bool(getattr(st, "st_file_attributes", 0) & 0x400) or stat_mod.S_ISLNK(st.st_mode)


def _kind(st: os.stat_result) -> str:
    if _is_reparse(st):
        return "link"
    if stat_mod.S_ISDIR(st.st_mode):
        return "dir"
    if stat_mod.S_ISREG(st.st_mode):
        return "file"
    return "other"


def _tracked(rel_parts: list[str]) -> bool:
    """Is the entry at this relative path part of the protected set?"""
    if not rel_parts:
        return False
    first = _norm(rel_parts[0])
    if first in ROOT_DIRS:
        return True
    if len(rel_parts) == 1 and first in ROOT_FILES:
        return True
    return _norm(rel_parts[-1]) in INSTRUCTION_FILES


def snapshot(root: str) -> Snapshot:
    """Walk `root` (never following reparse points; the top-level `.git` is skipped) and record every protected
    entry: kind, and for files size + sha256 (+ content, within MAX_SNAPSHOT_BYTES)."""
    entries: dict[str, Entry] = {}
    contents: dict[str, bytes] = {}
    budget = MAX_SNAPSHOT_BYTES
    stack: list[list[str]] = [[]]
    seen = 0
    while stack:
        parts = stack.pop()
        d = os.path.join(root, *parts) if parts else root
        try:
            items = list(os.scandir(d))
        except OSError as exc:
            raise OSError(f"cannot enumerate agent configuration under {d}: {exc}") from exc
        for e in items:
            seen += 1
            if seen > MAX_ENTRIES:
                raise OSError(f"more than {MAX_ENTRIES} entries in {root}")
            if not parts and _norm(e.name) == ".git":
                continue
            child = [*parts, e.name]
            try:
                st = e.stat(follow_symlinks=False)
            except OSError as exc:
                raise OSError(f"cannot inspect agent configuration entry {e.path}: {exc}") from exc
            kind = _kind(st)
            if _tracked(child):
                rel = "/".join(child)
                if kind == "file":
                    data = _read_nofollow(root, rel)
                    if len(data) > budget:
                        raise OSError("agent configuration exceeds the restorable snapshot budget")
                    entries[rel] = Entry("file", hashlib.sha256(data).hexdigest(), len(data))
                    contents[rel] = data
                    budget -= len(data)
                else:
                    entries[rel] = Entry(kind)
            if kind == "dir":
                stack.append(child)
    return Snapshot(entries, contents)


def _read_nofollow(root: str, rel: str) -> bytes:
    try:
        return path_policy.read_inside(root, rel, max_bytes=MAX_SNAPSHOT_BYTES)
    except (OSError, path_policy.PathPolicyError) as exc:
        raise OSError(f"cannot snapshot protected file {rel}: {exc}") from exc


def diff(before: Snapshot, after: Snapshot) -> list[str]:
    """Human-readable list of changes (sorted)."""
    out = []
    for rel in sorted(set(before.entries) | set(after.entries)):
        a, b = before.entries.get(rel), after.entries.get(rel)
        if a == b:
            continue
        if a is None:
            out.append(f"created {rel}")
        elif b is None:
            out.append(f"deleted {rel}")
        else:
            out.append(f"changed {rel}")
    return out


def restore(root: str, before: Snapshot, after: Snapshot) -> list[str]:
    """Bring the protected set back to `before`. Never follows a junction/symlink: links are removed as links,
    files are written through `paths.write_inside` (no reparse point on any component). Returns failures."""
    from orchestrator.security import sandbox

    failures: list[str] = []
    changed = [rel for rel in set(before.entries) | set(after.entries) if before.entries.get(rel) != after.entries.get(rel)]
    # 1. remove what is new or of the wrong kind (deepest first)
    for rel in sorted(changed, key=lambda r: r.count("/"), reverse=True):
        b, a = before.entries.get(rel), after.entries.get(rel)
        if a is None:
            continue
        if b is not None and b.kind == "dir" and a.kind == "dir":
            continue
        full = os.path.join(root, *rel.split("/"))
        try:
            with path_policy.guarded_parent(root, rel) as full:
                sandbox.remove_tree(full)
        except (OSError, path_policy.PathPolicyError) as exc:
            failures.append(f"remove {rel}: {exc}")
    # 2. recreate what was there (parents first)
    for rel in sorted(changed, key=lambda r: r.count("/")):
        b = before.entries.get(rel)
        if b is None:
            continue
        try:
            if b.kind == "dir":
                path_policy.make_dir_inside(root, rel)
            elif b.kind == "file":
                data = before.contents.get(rel)
                if data is None:
                    failures.append(f"{rel}: too large to restore")
                    continue
                path_policy.write_inside(root, rel, data)
            else:
                failures.append(f"{rel}: was a {b.kind}; not recreated")
        except (OSError, path_policy.PathPolicyError) as exc:
            failures.append(f"restore {rel}: {exc}")
    return failures


__all__ = ["INSTRUCTION_FILES", "ROOT_DIRS", "ROOT_FILES", "Snapshot", "diff", "restore", "snapshot"]
