"""Thin git wrapper. Arg arrays only, no shell, bounded by timeouts.

Functions are synchronous; call them from async code with `await agit(fn, ...)`
(runs in a worker thread so the event loop type does not matter).

Repository-controlled code execution (security finding #6, re-review N4, N2 re-review S1/S2).

1. No repository discovery, ever. Every git call gets an explicit `--git-dir` and `--work-tree` resolved by
   `resolve()` from trusted state, never from a file inside a task worktree:
   * a task worktree (a directory registered by `worktrees.create`, or anything under a registered worktrees
     root) uses git's administrative dir `<repo common dir>/worktrees/<name>`, found by scanning the common dirs
     of the registered repositories for the entry whose `gitdir` file points back at the worktree. The
     worktree's own `.git` link file is never read: replacing it (pointer to a planted git dir, a directory, a
     junction) or deleting it changes nothing for orchestrator git. Unresolvable -> `GitError` (fail closed).
   * any other path (a registered project folder, an orchestrator-owned scratch repo) uses the nearest `.git`
     directory found by walking up with `lstat`; a `.git` that is a reparse point is refused. A `.git` *file*
     there (the user's project is itself a linked worktree or a submodule) is followed - such a folder is the
     user's own, not a directory a sandboxed process can write.
2. Which configuration git then reads, and who can write it (see `CONFIG_SOURCES`): none of it is writable by
   a sandboxed verification command (Low integrity) or by a confined agent. Submodule configuration is never
   read: `--ignore-submodules=all` on status/diff plus the submodule keys in `HARDENING_CONFIG`, and
   `commit_all` refuses to create gitlinks (`UnsafeWorktree`).
3. `HARDENING_CONFIG` as `-c` options (command-line config is read last, so it wins over system, global and
   repository config for single-valued keys), plus `-c <key>=` for every filter / merge-driver / diff-driver
   command configured in the repository: defence in depth against the user's own repository config (git-lfs
   style filters), read from the same trusted config the command then uses (no attacker-writable source, so no
   scan/use race). Global/system attribute files are not read (`core.attributesFile=`, `GIT_ATTR_NOSYSTEM`); a
   worktree `.gitattributes` can only *name* a driver, whose command must come from config.
4. Every git child runs inside the worker Job Object (`worker_job.run_confined`).
5. No git call runs on a worktree while a sandboxed verification command executes in it (`worktree_busy`):
   callers wait briefly and then get `WorktreeBusy`.

Config includes cannot be neutralised reliably with a command-line override. Filtering commands therefore
refuse repositories/global configurations with include.path or includeIf.*.path, before following those includes.
This is deliberately fail closed: an include target can be writable even when its including file is protected.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import stat as stat_mod
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, TypeVar

from orchestrator.security import worker_job

T = TypeVar("T")

CHECKPOINT_AUTHOR_NAME = "AI Orchestrator"
CHECKPOINT_AUTHOR_EMAIL = "orchestrator@localhost"
DEFAULT_TIMEOUT = 120.0
BUSY_WAIT_SECONDS = 3.0
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_REPARSE = getattr(stat_mod, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class GitError(Exception):
    def __init__(self, args: list[str], returncode: Optional[int], stderr: str):
        super().__init__(f"git {' '.join(args[:3])}... failed ({returncode}): {stderr.strip()[:500]}")
        self.git_args = args
        self.returncode = returncode
        self.stderr = stderr


class NotARepository(GitError):
    """No trusted git dir for the path (not a repository, or an unresolvable task worktree)."""


class UnsafeWorktree(GitError):
    """The worktree holds something orchestrator git must never process (an embedded repository that would
    become a gitlink, i.e. a submodule whose own config git would read). A security finding: the task is blocked."""

    def __init__(self, detail: str):
        super().__init__(["add"], None, detail)
        self.detail = detail


class WorktreeBusy(GitError):
    """A sandboxed verification command is running in the worktree; no orchestrator git call runs meanwhile."""

    def __init__(self, path: str):
        super().__init__(["(busy)"], None, f"a verification command is running in {path}")


class QuarantinedWorktree(UnsafeWorktree):
    """A potentially live writer forbids Git and automatic filesystem cleanup."""


# Who can write each configuration source orchestrator git reads (N2 re-review S1). "Low" = a sandboxed
# verification command; "agent" = a provider CLI run (Medium integrity, confined by the orchestrator / its own
# sandbox to the task worktree and %TEMP%).
CONFIG_SOURCES: tuple[tuple[str, str], ...] = (
    ("system: <git install>/etc/gitconfig", "administrators only (Program Files)"),
    ("global: ~/.gitconfig, ~/.config/git/config", "the user (Medium); not Low (no-write-up), not a confined agent"),
    ("repository: <repo>/.git/config (+ config.worktree in the admin dir)",
     "the user (Medium); not Low; not a confined agent (Codex writes only worktree+TEMP; Claude file tools are "
     "confined to the worktree). Residual: an unconfined agent shell (claude_shell on) or an in_place project"),
    ("include.path / includeIf targets", "refused before filtering commands; target writers need not match the includer"),
    ("GIT_CONFIG_* / GIT_DIR / GIT_* environment", "never inherited (_env drops every GIT_* variable)"),
    ("-c command line", "the orchestrator only (HARDENING_CONFIG, driver overrides)"),
    ("submodule configs (<sub>/.git/config)", "never read: --ignore-submodules=all, no gitlinks are committed"),
    (".gitmodules", "not configuration; only read by submodule commands, which are never run"),
    ("worktree .git link file", "never read: --git-dir is always explicit"),
)


# Environment variables a git child may inherit. Nothing else (in particular no GIT_* variable such as
# GIT_DIR, GIT_CONFIG_*, GIT_EXTERNAL_DIFF, GIT_SSH_COMMAND, GIT_ASKPASS) is passed through from os.environ.
_ENV_ALLOWLIST = frozenset(
    {
        "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
        "USERPROFILE", "HOME", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
        "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "USERNAME", "USERDOMAIN", "COMPUTERNAME",
        "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "LANG", "TZ", "TERM",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR",
    }
)
_ENV_ALLOW_PREFIXES = ("LC_",)

# Applied to every git call, before the caller's own arguments (a caller's later `-c` wins, which the skills
# fetch uses to allow https for its explicit remote operation). Repository-controlled code execution is
# switched off: hooks, fsmonitor, pagers, ssh/ext transports, credential helpers, external diff drivers,
# submodule recursion.
HARDENING_CONFIG: tuple[tuple[str, str], ...] = (
    ("core.quotepath", "off"),
    ("core.hooksPath", os.devnull),
    ("core.fsmonitor", "false"),
    ("core.untrackedCache", "false"),
    ("core.pager", "cat"),
    ("core.editor", "true"),
    ("sequence.editor", "true"),
    ("core.askPass", ""),
    ("core.sshCommand", "false"),
    ("core.alternateRefsCommand", ""),
    ("core.attributesFile", ""),  # no global attributes file (GIT_ATTR_NOSYSTEM drops the system one)
    ("credential.helper", ""),
    ("protocol.ext.allow", "never"),
    ("protocol.git.allow", "never"),  # core.gitProxy is first-match-wins: a repo value cannot be overridden by -c
    # Signing / signature verification run gpg.program (or the ssh / x509 program): re-review N4.
    ("commit.gpgSign", "false"),
    ("tag.gpgSign", "false"),
    ("log.showSignature", "false"),
    ("gpg.program", ""),
    ("gpg.openpgp.program", ""),
    ("gpg.ssh.program", ""),
    ("gpg.x509.program", ""),
    ("gpg.ssh.defaultKeyCommand", ""),
    ("diff.external", ""),
    # Submodules (N2 re-review S1): a submodule's own config (filters, fsmonitor) is read by any git process
    # that recurses into it. Never recurse, never summarise, never look inside.
    ("diff.ignoreSubmodules", "all"),
    ("status.submoduleSummary", "false"),
    ("submodule.recurse", "false"),
    ("fetch.recurseSubmodules", "false"),
    # No background work started from our calls (auto gc / maintenance spawn further git processes).
    ("gc.auto", "0"),
    ("maintenance.auto", "false"),
)

# Subcommands the orchestrator runs. argv[0] after the options is always one of these literals, never a value
# from a repository, a request or a user (an unknown name could resolve to a repository-defined `alias.*`).
KNOWN_COMMANDS = frozenset(
    {"add", "cat-file", "check-ignore", "clean", "commit", "config", "diff", "fetch", "init", "log", "ls-files",
     "ls-tree", "remote", "reset", "rev-parse", "show", "show-ref", "status", "symbolic-ref", "worktree"}
)

# Commands that can run clean/smudge filters, i.e. a `filter.<name>.*` command planted in .git/config.
_FILTERING_COMMANDS = frozenset(
    {"add", "status", "diff", "commit", "reset", "checkout", "switch", "stash", "worktree", "merge", "restore",
     "clean", "cherry-pick", "rebase", "apply", "am", "archive", "show", "log", "ls-files"}
)
# Commands that would look inside a submodule: always told not to.
_SUBMODULE_BLIND = frozenset({"status", "diff"})
_WORKTREE_IO_COMMANDS = frozenset({"add", "status", "diff", "commit", "reset", "clean"})


def _env() -> dict[str, str]:
    """Allowlisted environment for git children (never any inherited GIT_* variable)."""
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper.startswith("GIT_"):
            continue
        if upper in _ENV_ALLOWLIST or upper.startswith(_ENV_ALLOW_PREFIXES):
            env[key] = value
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_PAGER"] = "cat"
    env["GIT_EDITOR"] = "true"
    env["GIT_ATTR_NOSYSTEM"] = "1"
    return env


def hardening_args() -> list[str]:
    out: list[str] = []
    for key, value in HARDENING_CONFIG:
        out += ["-c", f"{key}={value}"]
    return out


def git_prefix() -> list[str]:
    """argv prefix for every orchestrator git call: executable, no pager, hardening config."""
    return ["git", "--no-pager", *hardening_args()]


def _command_index(args: list[str]) -> Optional[int]:
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-c", "-C"):
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            return i
    return None


def _command_of(args: list[str]) -> Optional[str]:
    i = _command_index(args)
    return None if i is None else args[i]


# Repository-defined driver commands: clean/smudge filters, merge drivers, external diff and textconv drivers.
DRIVER_KEYS_REGEXP = r"^(filter\..*\.(clean|smudge|process|required)|merge\..*\.driver|diff\..*\.(command|textconv))$"
_SAFE_KEY = re.compile(r"^[A-Za-z0-9._-]+$")


def _run(argv: list[str], cwd: str | Path, *, timeout: float, input_text: Any = None, text: bool = True,
         related_worktrees: tuple[str, ...] = ()) -> subprocess.CompletedProcess[Any]:
    """Every git child: allowlisted env, no window, confined in the worker job before it runs."""
    with _git_operation(cwd):
        try:
            return worker_job.run_confined(
                argv, cwd=str(cwd), env=_env(), timeout=timeout, input=input_text,
                encoding="utf-8" if text else None, errors="replace" if text else None, creationflags=_NO_WINDOW,
            )
        except worker_job.ContainmentError as exc:
            for path in (str(cwd), *related_worktrees):
                quarantine_worktree(path, f"Git worker exit unverified: {exc}")
            raise QuarantinedWorktree(f"Git worker exit unverified: {exc}") from exc


# --------------------------------------------------------------------------------------------- trusted targets


@dataclass(frozen=True)
class GitTarget:
    """Where git must look: an explicit git dir and work tree, resolved from trusted state only."""

    git_dir: str
    work_tree: str
    cwd: str
    is_task_worktree: bool = False

    def args(self) -> list[str]:
        return [f"--git-dir={self.git_dir}", f"--work-tree={self.work_tree}"]


def _key(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


_lock = threading.Lock()
_worktree_roots: dict[str, str] = {}  # key -> real path of a directory that holds task worktrees
_repo_common_dirs: dict[str, str] = {}  # key -> real path of a registered repository's common git dir
_worktrees: dict[str, tuple[str, str]] = {}  # worktree key -> (real worktree path, admin git dir)


def _is_reparse(st: os.stat_result) -> bool:
    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE) or stat_mod.S_ISLNK(st.st_mode)


def _read_gitfile(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            line = fh.read(4096).strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    target = line[len("gitdir:"):].strip()
    if not os.path.isabs(target):
        target = os.path.join(os.path.dirname(path), target)
    return os.path.normpath(target)


def common_dir_of(repo: str | os.PathLike[str]) -> Optional[str]:
    """The common git dir of a (non-worktree) repository path, without running git. None if not a repository."""
    target = _discover(os.path.realpath(os.fspath(repo)))
    if target is None:
        return None
    gd = target.git_dir
    commondir = os.path.join(gd, "commondir")
    if os.path.isfile(commondir):
        try:
            rel = Path(commondir).read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return os.path.realpath(os.path.join(gd, rel))
    return os.path.realpath(gd)


def register_worktree_root(path: str | os.PathLike[str]) -> None:
    """Every path under `path` is treated as a task worktree: its git dir comes from a registered repository's
    admin dir, never from the directory itself."""
    real = os.path.realpath(os.fspath(path))
    with _lock:
        _worktree_roots[os.path.normcase(real)] = real


def register_repo(repo: str | os.PathLike[str]) -> Optional[str]:
    """Remember a project repository (its common git dir is scanned to resolve task worktrees)."""
    common = common_dir_of(repo)
    if common is not None:
        with _lock:
            _repo_common_dirs[os.path.normcase(common)] = common
    return common


def _admin_in(common: str, wt_real: str) -> Optional[str]:
    """The admin dir under `common/worktrees` whose `gitdir` file points at `<wt_real>/.git`."""
    base = os.path.join(common, "worktrees")
    want = os.path.normcase(os.path.normpath(wt_real))
    try:
        entries = list(os.scandir(base))
    except OSError:
        return None
    for entry in entries:
        try:
            if _is_reparse(entry.stat(follow_symlinks=False)) or not entry.is_dir(follow_symlinks=False):
                continue
            with open(os.path.join(entry.path, "gitdir"), "r", encoding="utf-8", errors="replace") as fh:
                pointed = fh.read(4096).strip()
        except OSError:
            continue
        if not pointed:
            continue
        parent = os.path.dirname(os.path.normpath(pointed))
        if os.path.normcase(os.path.realpath(parent)) == want or os.path.normcase(os.path.normpath(parent)) == want:
            return entry.path
    return None


def register_worktree(repo: str | os.PathLike[str], wt_path: str | os.PathLike[str]) -> str:
    """Bind a task worktree to its admin git dir in `repo` (found in the repository's own git dir, never through
    the worktree). Raises NotARepository when git has no record of it."""
    common = register_repo(repo)
    real = os.path.realpath(os.fspath(wt_path))
    admin = _admin_in(common, real) if common else None
    if admin is None:
        raise NotARepository(["worktree"], None, f"{wt_path} is not a registered worktree of {repo}")
    with _lock:
        _worktrees[os.path.normcase(real)] = (real, admin)
    return admin


def forget_worktree(wt_path: str | os.PathLike[str]) -> None:
    with _lock:
        _worktrees.pop(_key(wt_path), None)


def _inside(key: str, root_key: str) -> bool:
    return key == root_key or key.startswith(root_key.rstrip("\\/") + os.sep)


def _discover(real: str) -> Optional[GitTarget]:
    """Nearest `.git` above a non-worktree path, by lstat (git's own discovery is never used)."""
    d = real
    for _ in range(256):
        g = os.path.join(d, ".git")
        try:
            st = os.lstat(g)
        except OSError:
            st = None
        if st is not None:
            if _is_reparse(st):
                raise NotARepository(["rev-parse"], None, f"refusing {g}: it is a junction/symlink")
            if stat_mod.S_ISDIR(st.st_mode):
                return GitTarget(git_dir=g, work_tree=d, cwd=real)
            pointed = _read_gitfile(g)
            if pointed is None:
                return None
            return GitTarget(git_dir=pointed, work_tree=d, cwd=real)
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    return None


def resolve(path: str | os.PathLike[str] | GitTarget) -> GitTarget:
    """The trusted git dir / work tree for `path` (see the module docstring). Raises NotARepository."""
    if isinstance(path, GitTarget):
        return path
    real = os.path.realpath(os.fspath(path))
    key = os.path.normcase(real)
    with _lock:
        for wt_key, (wt_real, admin) in _worktrees.items():
            if _inside(key, wt_key):
                return GitTarget(git_dir=admin, work_tree=wt_real, cwd=real, is_task_worktree=True)
        roots = list(_worktree_roots.items())
        commons = list(_repo_common_dirs.values())
    for root_key, root_real in roots:
        if _inside(key, root_key):
            first = "" if key == root_key else os.path.relpath(real, root_real).split(os.sep)[0]
            if not first or first in (".", ".."):
                raise NotARepository(["rev-parse"], None, f"{path} is the worktrees directory itself")
            wt_real = os.path.join(root_real, first)
            for common in commons:
                admin = _admin_in(common, wt_real)
                if admin is not None:
                    with _lock:
                        _worktrees[os.path.normcase(wt_real)] = (wt_real, admin)
                    return GitTarget(git_dir=admin, work_tree=wt_real, cwd=real, is_task_worktree=True)
            raise NotARepository(["rev-parse"], None,
                                 f"{path} is inside the worktrees directory but no registered repository owns it")
    target = _discover(real)
    if target is None:
        raise NotARepository(["rev-parse"], None, f"{path} is not inside a git repository")
    return target


def target_args(path: str | os.PathLike[str] | GitTarget) -> list[str]:
    """`--git-dir=... --work-tree=...` for a raw git invocation (callers outside this module)."""
    return resolve(path).args()


# --------------------------------------------------------------------------------------------- busy worktrees

_busy: dict[str, int] = {}
_active_git: dict[str, int] = {}
_git_owners: dict[str, int] = {}
_quarantined: dict[str, str] = {}
_busy_cond = threading.Condition()


def _overlap(a: str, b: str) -> bool:
    return _inside(a, b) or _inside(b, a)


def quarantine_worktree(path: str | os.PathLike[str], reason: str) -> None:
    """Refuse further use when worker exit cannot be proven, until server restart.

    The process-wide worker job kills all descendants on server exit. There is
    deliberately no API to clear a quarantine in the still-running server.
    """
    with _busy_cond:
        _quarantined[_key(path)] = reason[:500]
        _busy_cond.notify_all()


def _refuse_quarantine(key: str) -> None:
    for path, reason in _quarantined.items():
        if _overlap(key, path):
            raise QuarantinedWorktree(f"worktree quarantined: worker exit was not verified: {reason}")


@contextlib.contextmanager
def _git_operation(path: str | os.PathLike[str]) -> Iterator[None]:
    """Reserve a git subprocess atomically against verification, including raw plumbing calls."""
    k = _key(path)
    owner = threading.get_ident()
    deadline = time.monotonic() + BUSY_WAIT_SECONDS
    with _busy_cond:
        _refuse_quarantine(k)
        while (any(_overlap(k, b) for b in _busy)
               or any(_overlap(k, a) and _git_owners[a] != owner for a in _active_git)):
            left = deadline - time.monotonic()
            if left <= 0:
                raise WorktreeBusy(os.fspath(path))
            _busy_cond.wait(left)
            _refuse_quarantine(k)
        _active_git[k] = _active_git.get(k, 0) + 1
        _git_owners[k] = owner
    try:
        yield
    finally:
        with _busy_cond:
            n = _active_git[k] - 1
            if n:
                _active_git[k] = n
            else:
                del _active_git[k]
                del _git_owners[k]
            _busy_cond.notify_all()


@contextlib.contextmanager
def worktree_busy(path: str | os.PathLike[str]) -> Iterator[None]:
    """Exclusive verification/removal reservation; waits for git already running before entering."""
    k = _key(path)
    deadline = time.monotonic() + BUSY_WAIT_SECONDS
    with _busy_cond:
        _refuse_quarantine(k)
        while any(_overlap(k, b) for b in _busy) or any(_overlap(k, b) for b in _active_git):
            left = deadline - time.monotonic()
            if left <= 0:
                raise WorktreeBusy(os.fspath(path))
            _busy_cond.wait(left)
            _refuse_quarantine(k)
        _busy[k] = _busy.get(k, 0) + 1
    try:
        yield
    finally:
        with _busy_cond:
            n = _busy.get(k, 0) - 1
            if n <= 0:
                _busy.pop(k, None)
            else:
                _busy[k] = n
            _busy_cond.notify_all()


def is_busy(path: str | os.PathLike[str]) -> bool:
    k = _key(path)
    with _busy_cond:
        return any(_inside(k, b) for b in _busy) or any(_overlap(k, b) for b in _quarantined)


def _wait_idle(work_tree: str, timeout: float) -> None:
    k = os.path.normcase(work_tree)
    deadline = time.monotonic() + timeout
    with _busy_cond:
        _refuse_quarantine(k)
        while any(_inside(k, b) or _inside(b, k) for b in _busy):
            left = deadline - time.monotonic()
            if left <= 0:
                raise WorktreeBusy(work_tree)
            _busy_cond.wait(left)
            _refuse_quarantine(k)


# --------------------------------------------------------------------------------------------- running git


def _filter_overrides(path: str | os.PathLike[str] | GitTarget) -> list[str]:
    """`-c <key>=` for every filter / merge driver / diff driver command configured in the repository (and
    `filter.<name>.required=false`), so a planted command cannot run on add/status/diff/commit/merge."""
    target = resolve(path)
    try:
        includes = _run([*git_prefix(), *target.args(), "config", "--no-includes", "--name-only", "--get-regexp",
                         r"^include(if\..*)?\.path$"], target.cwd, timeout=30)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise GitError(["config"], None, f"cannot inspect Git include policy: {exc}") from exc
    if includes.returncode == 0:
        raise UnsafeWorktree("Git configuration includes are unsupported for checkpoint/filtering operations; "
                             "included files may be agent-writable and cannot be safely overridden")
    if includes.returncode != 1:
        raise GitError(["config"], includes.returncode, includes.stderr)
    try:
        proc = _run([*git_prefix(), *target.args(), "config", "--name-only", "--get-regexp", DRIVER_KEYS_REGEXP],
                    target.cwd, timeout=30)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise GitError(["config"], None, f"cannot inspect repository driver configuration: {exc}") from exc
    if proc.returncode not in (0, 1):  # 1 means that no matching driver keys exist
        raise GitError(["config"], proc.returncode, proc.stderr)
    out: list[str] = []
    for name in sorted({ln.strip() for ln in proc.stdout.splitlines() if ln.strip()}):
        if not _SAFE_KEY.match(name):  # a subsection with odd characters cannot be overridden safely
            raise GitError(["config"], None, f"refusing to run git: unsupported driver key in repository config: {name!r}")
        out += ["-c", f"{name}=false" if name.endswith(".required") else f"{name}="]
    return out


def assert_plain_worktree(root: str | os.PathLike[str], *, max_entries: int = 2_000_000,
                          include_git: bool = False) -> None:
    """Medium Git must not follow a junction or read a hardlinked payload file.

    The top-level .git entry is deliberately excluded: task Git uses its trusted
    administrative directory, not this mutable link. Call under a Git reservation
    after all untrusted writers have stopped, including provider processes.
    Other consumers must opt into include_git when they can access .git themselves.
    """
    stack = [os.fspath(root)]
    seen = 0
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if not include_git and directory == os.fspath(root) and _is_dot_git(entry.name):
                        continue
                    seen += 1
                    if seen > max_entries:
                        raise UnsafeWorktree("worktree traversal limit exceeded before Git access")
                    # Windows DirEntry's cached stat can report st_nlink=0.
                    # A fresh no-follow stat is required for the hardlink boundary.
                    st = os.stat(entry.path, follow_symlinks=False)
                    if _is_reparse(st) or (stat_mod.S_ISREG(st.st_mode) and st.st_nlink > 1):
                        raise UnsafeWorktree("Git refuses a junction, symlink or hardlinked payload: "
                                             + os.path.relpath(entry.path, root))
                    if stat_mod.S_ISDIR(st.st_mode):
                        stack.append(entry.path)
        except OSError as exc:
            raise UnsafeWorktree(f"cannot inspect worktree before Git access: {exc}") from exc


def run_git(
    args: list[str],
    cwd: str | Path | GitTarget,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    check: bool = True,
    input_text: Optional[str] = None,
    busy_wait: float = BUSY_WAIT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    if not all(isinstance(a, str) for a in args):
        raise TypeError("git args must be strings")
    idx = _command_index(args)
    command = None if idx is None else args[idx]
    if command not in KNOWN_COMMANDS:
        raise ValueError(f"unsupported git subcommand {command!r}")
    assert idx is not None
    args = list(args)
    related_worktrees: tuple[str, ...] = ()
    if command == "worktree" and args[idx + 1:idx + 2] in (["add"], ["remove"]):
        position = idx + 2
        while position < len(args):
            value = args[position]
            if value in {"-b", "-B"}:
                position += 2
            elif value.startswith("-"):
                position += 1
            else:
                related_worktrees = (args[position],)
                break
    if command in _SUBMODULE_BLIND and "--ignore-submodules=all" not in args:
        args.insert(idx + 1, "--ignore-submodules=all")
    try:
        with contextlib.ExitStack() as held:
            if command == "init":  # no repository exists yet
                run_cwd = cwd.cwd if isinstance(cwd, GitTarget) else str(cwd)
                argv = [*git_prefix(), *args]
            else:
                target = resolve(cwd)
                run_cwd = target.cwd
                if target.is_task_worktree:
                    _wait_idle(target.work_tree, busy_wait)
                # One reservation spans include checks, the driver scan and use.
                # The raw _run reservation remains for plumbing callers.
                held.enter_context(_git_operation(target.work_tree))
                if command in _WORKTREE_IO_COMMANDS:
                    assert_plain_worktree(target.work_tree)
                elif command == "worktree" and args[idx + 1:idx + 2] == ["remove"]:
                    assert_plain_worktree(args[-1])
                argv = [*git_prefix(), *target.args(), *args]
                if command in _FILTERING_COMMANDS:
                    argv = [*git_prefix(), *target.args(), *_filter_overrides(target), *args]
            proc = _run(argv, run_cwd, timeout=timeout, input_text=input_text,
                        related_worktrees=related_worktrees)
    except subprocess.TimeoutExpired as exc:
        raise GitError(args, None, f"timeout after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise GitError(args, None, "git executable not found") from exc
    except OSError as exc:  # includes a refused confinement (the process was killed before it ran)
        raise GitError(args, None, f"could not start git: {exc}") from exc
    if check and proc.returncode != 0:
        raise GitError(args, proc.returncode, proc.stderr)
    return proc


async def agit(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    return await asyncio.to_thread(fn, *args, **kwargs)


def is_repo(path: str | Path | GitTarget) -> bool:
    if not isinstance(path, GitTarget) and not Path(path).is_dir():
        return False
    try:
        target = resolve(path)
    except NotARepository:
        return False
    proc = run_git(["rev-parse", "--is-inside-work-tree"], target, check=False, timeout=30)
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def toplevel(path: str | Path | GitTarget) -> str:
    return run_git(["rev-parse", "--show-toplevel"], path).stdout.strip()


def rev_parse(path: str | Path | GitTarget, ref: str = "HEAD") -> Optional[str]:
    if ref.startswith("-"):
        raise ValueError("invalid ref")
    proc = run_git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], path, check=False)
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and sha else None


def current_branch(path: str | Path | GitTarget) -> Optional[str]:
    proc = run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], path, check=False)
    return proc.stdout.strip() or None


def status_porcelain(path: str | Path | GitTarget) -> list[tuple[str, str]]:
    out = run_git(["status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=all"], path).stdout
    entries = []
    for line in out.splitlines():
        if len(line) > 3:
            entries.append((line[:2], line[3:]))
    return entries


def has_changes(path: str | Path | GitTarget) -> bool:
    return bool(status_porcelain(path))


def diff(path: str | Path | GitTarget, base: Optional[str] = None, *, stat: bool = False, max_chars: int = 200_000) -> str:
    args = ["diff", "--no-color", "--no-ext-diff", "--no-textconv", "--ignore-submodules=all"]
    if stat:
        args.append("--stat")
    if base:
        if base.startswith("-"):
            raise ValueError("invalid ref")
        args.append(base)
    args.append("--")
    text = run_git(args, path).stdout
    return text if len(text) <= max_chars else text[:max_chars] + "\n...[diff truncated]...\n"


def changed_files(path: str | Path | GitTarget, base: Optional[str] = None) -> list[str]:
    """Files differing from base (committed or not) plus untracked files."""
    files: set[str] = set()
    if base:
        out = run_git(["diff", "--name-only", "--no-ext-diff", "--no-textconv", "--ignore-submodules=all", base, "--"],
                      path).stdout
        files.update(line.strip() for line in out.splitlines() if line.strip())
    for code, name in status_porcelain(path):
        if " -> " in name:
            name = name.split(" -> ", 1)[1]
        files.add(name.strip().strip('"'))
    return sorted(files)


def log_oneline(path: str | Path | GitTarget, rev_range: str, max_count: int = 20) -> list[str]:
    if rev_range.startswith("-"):
        raise ValueError("invalid range")
    proc = run_git(["log", "--no-ext-diff", "--no-textconv", "--format=%h %s", f"--max-count={int(max_count)}", rev_range, "--"], path, check=False)
    return [line for line in proc.stdout.splitlines() if line.strip()] if proc.returncode == 0 else []


def _identity_args() -> list[str]:
    return ["-c", f"user.name={CHECKPOINT_AUTHOR_NAME}", "-c", f"user.email={CHECKPOINT_AUTHOR_EMAIL}"]


def _is_dot_git(name: str) -> bool:
    # NTFS ignores trailing dots and spaces and is case-insensitive: ".GIT", ".git." and ".git " all open ".git".
    return name.rstrip(" .").casefold() == ".git"


def nested_repositories(work_tree: str | os.PathLike[str], *, max_entries: int = 2_000_000) -> list[str]:
    """Relative posix paths of directories below `work_tree` that contain a `.git` entry (file, directory or
    link): `git add` would record each as a gitlink (embedded repository). Never follows reparse points; the
    top-level `.git` (the worktree's own link) is not reported."""
    root = os.fspath(work_tree)
    found: list[str] = []
    stack = [""]
    seen = 0
    while stack:
        rel = stack.pop()
        try:
            entries = list(os.scandir(os.path.join(root, rel) if rel else root))
        except OSError:
            continue
        for e in entries:
            seen += 1
            if seen > max_entries:
                raise UnsafeWorktree(f"worktree has more than {max_entries} entries; refusing to commit it")
            child = f"{rel}/{e.name}" if rel else e.name
            if _is_dot_git(e.name):
                if rel:
                    found.append(rel)
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat_mod.S_ISDIR(st.st_mode) and not _is_reparse(st):
                stack.append(child)
    return sorted(set(found))


def _not_ignored(target: GitTarget, rels: list[str]) -> list[str]:
    """The subset of `rels` (directories) that git would not skip as ignored."""
    if not rels:
        return []
    proc = run_git(["check-ignore", "--stdin", "-z"], target, check=False,
                   input_text="".join(r.rstrip("/") + "/\0" for r in rels))
    ignored = {p.rstrip("/") for p in proc.stdout.split("\0") if p} if proc.returncode in (0, 1) else set()
    return [r for r in rels if r.rstrip("/") not in ignored]


def gitlinks_in_index(path: str | Path | GitTarget) -> list[str]:
    out = run_git(["ls-files", "--stage", "-z"], path).stdout
    links = []
    for rec in out.split("\0"):
        if rec.startswith("160000 "):
            links.append(rec.partition("\t")[2])
    return links


def commit_all(path: str | Path | GitTarget, message: str) -> Optional[str]:
    """Stage everything and commit with the fixed checkpoint identity.

    Refuses (UnsafeWorktree) to stage an embedded repository: a gitlink would make later status/diff calls
    consult that repository's own config (filters, fsmonitor) - which whoever wrote the worktree controls.
    Checked before `add` (filesystem scan) and after it (no mode-160000 entry may be in the index; the index is
    reset when one is). Returns the new HEAD sha, or None when there was nothing to commit."""
    target = resolve(path)
    nested = _not_ignored(target, nested_repositories(target.work_tree))
    if nested:
        raise UnsafeWorktree("embedded git repositories in the worktree (would become submodules): "
                             + ", ".join(nested[:10]))
    run_git(["add", "-A"], target)
    links = gitlinks_in_index(target)
    if links:
        run_git(["reset", "--quiet"], target, check=False)
        raise UnsafeWorktree("git staged gitlink (submodule) entries: " + ", ".join(links[:10]))
    staged = run_git(["diff", "--cached", "--quiet", "--no-ext-diff", "--no-textconv"], target, check=False)
    if staged.returncode == 0:
        return None
    author = f"{CHECKPOINT_AUTHOR_NAME} <{CHECKPOINT_AUTHOR_EMAIL}>"
    run_git([*_identity_args(), "commit", "--quiet", "--no-verify", "--no-gpg-sign", f"--author={author}", "-F", "-"], target, input_text=message)
    return rev_parse(target, "HEAD")


def init_repo(path: str | Path, message: str = "Initial commit (AI Orchestrator)") -> Optional[str]:
    run_git(["init", "--quiet"], path)
    if commit_all(path, message) is None:
        run_git([*_identity_args(), "commit", "--quiet", "--no-verify", "--no-gpg-sign", "--allow-empty", "-m", message], path)
    return rev_parse(path, "HEAD")


def reset_hard(path: str | Path | GitTarget, ref: str) -> None:
    """Only ever called on orchestrator-owned worktrees (callers enforce this)."""
    if ref.startswith("-"):
        raise ValueError("invalid ref")
    run_git(["reset", "--hard", "--quiet", ref], path)
    run_git(["clean", "-fd", "--quiet"], path)
