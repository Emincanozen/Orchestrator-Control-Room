"""Per-task git worktrees (ADR-006): <worktrees dir>/<task_id> on branch orch/<task_id> (ADR-022).

Every worktree is bound to its administrative git dir in the project repository (`git.register_worktree`):
orchestrator git never reads the worktree's own `.git` link file (N2 re-review S2)."""

from __future__ import annotations

import logging
import os
import re
import time

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from orchestrator.gitops import git
from orchestrator.security import sandbox

log = logging.getLogger(__name__)

_SAFE_ID = re.compile(r"^[A-Za-z0-9_\-]{1,80}$")
LEGACY_DIR = "_legacy"  # <worktrees dir>/_legacy/: legacy-marked worktrees set aside (preserved, never used)
_NESTED_SCAN_LIMIT = 200_000
_INHERITED_ACE = 0x10


@dataclass
class WorktreeInfo:
    path: str
    branch: str
    base_commit: str
    head_commit: Optional[str]
    created: bool


def branch_name(task_id: str) -> str:
    return f"orch/{task_id}"


def worktree_path(worktrees_dir: Path, task_id: str) -> Path:
    if not _SAFE_ID.match(task_id):
        raise ValueError("unsafe task id for worktree path")
    git.register_worktree_root(worktrees_dir)
    return worktrees_dir / task_id


def _branch_exists(repo: str | Path, branch: str) -> bool:
    return git.run_git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], repo, check=False).returncode == 0


def _registered(repo: str | Path) -> dict[str, dict[str, str]]:
    git.register_repo(repo)
    out: dict[str, dict[str, str]] = {}
    text = git.run_git(["worktree", "list", "--porcelain"], repo).stdout
    current: dict[str, str] = {}
    for line in text.splitlines() + [""]:
        if not line.strip():
            if "worktree" in current:
                out[os.path.normcase(os.path.realpath(current["worktree"]))] = current
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value or "true"
    return out


def list_worktrees(repo: str | Path) -> list[dict[str, str]]:
    return list(_registered(repo).values())


def _pin_root(wt_path: Path) -> None:
    """The worktree root itself cannot be deleted, renamed or re-ACLed by anyone holding the user's SID - in
    particular not by a sandboxed process of another task (N2 re-review S5). The orchestrator still removes it
    through FILE_DELETE_CHILD on the (Medium) worktrees dir. Logged on failure; verification re-checks."""
    if not sandbox.supported():
        return
    try:
        sandbox.pin_directory(wt_path)
    except sandbox.SandboxUnavailable as exc:
        log.error("SECURITY: %s", exc)


def legacy_sandbox_marks(wt_path: Path) -> list[str]:
    """What a pre-copy-in version left on a worktree root: an explicit Low mandatory label and/or an explicit
    AppContainer (S-1-15-2-*) ACE. Both were applied to the EMPTY root and inherited by everything created below
    it, so the root is the witness for the whole tree. Only NON-inherited marks count: a label or package ACE
    inherited from a permissive ancestor is not something recreation can remove (the new root would inherit it
    again), so counting it would recreate or refuse the worktree forever. [] for a clean root (and off Windows)."""
    if not sandbox.supported():
        return []
    from orchestrator.security import acl

    marks = []
    try:
        label = sandbox.read_label(wt_path)
        aces = acl.read_security(wt_path)["aces"] or []
    except OSError as exc:
        return [f"cannot inspect the worktree security descriptor: {exc}"]
    if label is not None and label[0] < sandbox.MEDIUM_RID and not label[2] & _INHERITED_ACE:
        marks.append("Low integrity label")
    sids = sorted({a["sid"] for a in aces
                   if a["sid"] and a["sid"].startswith("S-1-15-2-") and not a.get("inherited")})
    if sids:
        marks.append("AppContainer grant " + ", ".join(sids))
    return marks


def _nested_git(wt_path: Path) -> Optional[str]:
    """The first `.git` entry below the worktree root (a submodule or nested repository: work there is not
    visible to a submodule-blind status), or None. Bounded; links are not followed."""
    import stat as stat_mod

    stack, seen = [str(wt_path)], 0
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                seen += 1
                if seen > _NESTED_SCAN_LIMIT:
                    return f"more than {_NESTED_SCAN_LIMIT} entries (not inspected)"
                if entry.name.casefold() == ".git" and directory != str(wt_path):
                    return os.path.relpath(entry.path, wt_path)
                st = entry.stat(follow_symlinks=False)
                if stat_mod.S_ISDIR(st.st_mode) and not sandbox.is_reparse(st) and entry.name.casefold() != ".git":
                    stack.append(entry.path)
    return None


def _set_aside_legacy(repo: str | Path, wt_path: Path, branch: str, marks: list[str]) -> Path:
    """Move a legacy-marked worktree into `<worktrees dir>/_legacy/` (preserved for the operator; nothing is
    deleted), provided it provably holds no work beyond its branch: HEAD is attached to `refs/heads/<branch>` at
    its tip, no uncommitted/untracked changes, no nested repository. The checks and the move run under one Git
    reservation of the worktree, so no verification, removal or other Git call can interleave. Raises
    UnsafeWorktree (nothing changed) otherwise."""
    prefix = "the worktree still carries the sandbox marks of an older version (" + "; ".join(marks) + ")"
    with git._git_operation(wt_path):
        attached = git.current_branch(wt_path)
        head, tip = git.rev_parse(wt_path), git.rev_parse(repo, f"refs/heads/{branch}")
        if attached != branch or head is None or head != tip:
            raise git.UnsafeWorktree(f"{prefix} and its HEAD is not the tip of {branch} (detached or diverged); "
                                     "resolve it manually before reuse")
        if git.has_changes(wt_path):
            raise git.UnsafeWorktree(f"{prefix} and has uncommitted changes; commit or discard them, or remove the "
                                     "worktree, before reuse")
        nested = _nested_git(wt_path)
        if nested is not None:
            raise git.UnsafeWorktree(f"{prefix} and contains a nested repository or submodule ({nested}); "
                                     "resolve it manually before reuse")
        legacy_root = sandbox.real_dir(wt_path.parent / LEGACY_DIR, create=True)
        stamp = time.strftime("%Y%m%d%H%M%S")
        aside = legacy_root / f"{wt_path.name}-{stamp}-{os.urandom(4).hex()}"
        try:
            os.rename(wt_path, aside)  # same parent tree, never replaces; open handles inside refuse it
        except OSError as exc:
            raise git.UnsafeWorktree(f"cannot set the legacy worktree aside ({exc}); it is left in place") from exc
    # The moved tree's `.git` link would point at the admin dir the recreated worktree reuses.
    # Random suffix: a planted fixed name cannot make the detach fail. If it still fails, refuse recreation
    # (fail closed) rather than leave a preserved tree linked to the admin dir a new worktree would reuse.
    try:
        os.rename(aside / ".git", aside / f".git.orchestrator-detached-{os.urandom(8).hex()}")
    except OSError as exc:
        raise git.UnsafeWorktree(f"legacy worktree set aside at {aside} but its .git link could not be "
                                 f"detached ({exc}); refusing to recreate the worktree") from exc
    return aside


def create(repo: str | Path, wt_path: Path, task_id: str, base_commit: str) -> WorktreeInfo:
    """Create (or reuse) the task worktree. Idempotent.

    A worktree labelled/granted by a pre-copy-in version is not reused as is: its root is pinned (the user's own
    WRITE_DAC / WRITE_OWNER are denied by design, so the label and ACE cannot be removed without privilege) and
    every entry below carries inherited copies, which only a path-walking relabel over agent-written content
    could strip (the hardlink hazard of `sandbox.label_low(tree=True)`). Instead it is set aside into
    `_legacy/` (preserved, not deleted) and recreated from its branch when HEAD is the branch tip and it has no
    uncommitted/untracked changes or nested repository; otherwise it is refused (UnsafeWorktree: the task blocks
    for review, nothing is lost)."""
    branch = branch_name(task_id)
    key = os.path.normcase(os.path.realpath(wt_path))
    registered = _registered(repo)
    if key in registered and wt_path.is_dir():
        git.register_worktree(repo, wt_path)
        head = git.rev_parse(wt_path)  # refuse quarantine before reuse
        marks = legacy_sandbox_marks(wt_path)
        if not marks:
            _pin_root(wt_path)
            return WorktreeInfo(str(wt_path), branch, base_commit, head, created=False)
        aside = _set_aside_legacy(repo, wt_path, branch, marks)
        log.warning("SECURITY: recreating worktree %s from branch %s (legacy sandbox marks: %s); the old tree is "
                    "preserved at %s", wt_path, branch, "; ".join(marks), aside)
        git.forget_worktree(wt_path)
        git.run_git(["worktree", "prune"], repo, check=False)
        registered = _registered(repo)
    if key in registered or wt_path.exists():
        # Half-created or orphaned directory: prune git's record and the dir (ours only).
        git.forget_worktree(wt_path)
        git.run_git(["worktree", "prune"], repo, check=False)
        if wt_path.exists():
            sandbox.remove_tree(wt_path)
    # The worktree receives no sandbox grant and no Low label: verification commands run in a disposable copy
    # (security/verify_tree.py) and can never write here.
    wt_path.mkdir(parents=True, exist_ok=False)
    if _branch_exists(repo, branch):
        git.run_git(["worktree", "add", "--quiet", str(wt_path), branch], repo)
    else:
        git.run_git(["worktree", "add", "--quiet", "-b", branch, str(wt_path), base_commit], repo)
    git.register_worktree(repo, wt_path)
    _pin_root(wt_path)
    return WorktreeInfo(str(wt_path), branch, base_commit, git.rev_parse(wt_path), created=True)


def remove(repo: str | Path, wt_path: Path) -> None:
    """Remove the worktree directory (orchestrator-owned). The branch is kept:
    deleting branches is an approval-gated destructive git operation."""
    with git.worktree_busy(wt_path):
        try:
            proc = git.run_git(["worktree", "remove", "--force", str(wt_path)], repo, check=False)
            needs_cleanup = proc.returncode != 0
        except git.QuarantinedWorktree:
            raise  # possibly live Git/worker writers: never attempt filesystem cleanup
        except git.UnsafeWorktree:
            # Git must never traverse hostile payload links. The existing
            # handle-guarded remover unlinks these entries without following them.
            needs_cleanup = True
        if needs_cleanup and os.path.lexists(wt_path):
            sandbox.remove_tree(wt_path)
        git.forget_worktree(wt_path)
        git.run_git(["worktree", "prune"], repo, check=False)


def stale(repo: str | Path, worktrees_dir: Path, live_task_ids: Iterable[str]) -> list[str]:
    """Worktree paths under worktrees_dir that no live (non-terminal) task owns, or that git marks prunable."""
    git.register_worktree_root(worktrees_dir)
    live = set(live_task_ids)
    root = os.path.normcase(os.path.realpath(worktrees_dir))
    out = []
    for key, info in _registered(repo).items():
        if not key.startswith(root + os.sep):
            continue
        task_id = Path(info["worktree"]).name
        if task_id not in live or "prunable" in info:
            out.append(info["worktree"])
    return sorted(out)


__all__ = ["WorktreeInfo", "branch_name", "create", "list_worktrees", "remove", "stale", "worktree_path"]
