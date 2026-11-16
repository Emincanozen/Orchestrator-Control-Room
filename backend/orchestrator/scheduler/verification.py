"""Deterministic verification (ARCHITECTURE §4, ADR-007).

Runs verify commands as arg arrays inside the task worktree, with timeouts and
process-tree kill, plus file-existence checks. Stores a `verifications` row and
emits `verification.result`. Output is truncated and redacted.

Accepted spec forms (task.verify_commands, falling back to project's):
    ["python", "-m", "pytest", "-q"]          list form (preferred)
    "python -m pytest -q"                     string form, split without a shell
    {"cmd": [...] | "...", "timeout": 60, "name": "unit"}
    {"file_exists": "dist/index.html", "contains": "<html"}
    {"file_absent": "tmp.txt"}  {"file_unchanged": "README.md"}  {"json_valid": "package.json"}
file_unchanged compares the worktree file with the task's base commit through git (no shell command).
String form refuses shell operators (| & ; < > ` $( ) because no shell is used.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import ntpath
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

import psutil

from orchestrator.config import child_env
from orchestrator.db import records
from orchestrator.db.database import Tx
from orchestrator.domain import events as ev
from orchestrator.gitops import git
from orchestrator.domain.models import CheckResult, Task, Verification, VerifyCheck
from orchestrator.security import sandbox, worker_job, timing
from orchestrator.security import paths as path_policy
from orchestrator.security.paths import PathPolicyError, resolve_inside
from orchestrator.security.redact import redact
from orchestrator.services.context import AppContext
from orchestrator.util import new_id, now_iso, truncate

log = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"
OUTPUT_LIMIT = 8000
MAX_OUTPUT_BYTES = 16 * 1024 * 1024  # a command producing more than this is stopped
POLL_SECONDS = 0.05
_SHELL_CHARS = set("|&;<>`")
_CMD_META = set('&|<>^%!"\r\n')


class CommandSpecError(ValueError):
    pass


# --------------------------------------------------------------------------- #
# Command splitting
# --------------------------------------------------------------------------- #


def _split_windows(cmd: str) -> list[str]:
    """CommandLineToArgvW-compatible split (MSVCRT 2008+ rules)."""
    args: list[str] = []
    cur: list[str] = []
    in_quotes = False
    have_arg = False
    i, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        if c == "\\":
            j = i
            while j < n and cmd[j] == "\\":
                j += 1
            count = j - i
            if j < n and cmd[j] == '"':
                cur.append("\\" * (count // 2))
                if count % 2 == 1:
                    cur.append('"')
                    i = j + 1
                else:
                    i = j  # the quote is processed next
                have_arg = True
                continue
            cur.append("\\" * count)
            have_arg = True
            i = j
            continue
        if c == '"':
            if in_quotes and i + 1 < n and cmd[i + 1] == '"':
                cur.append('"')  # "" inside quotes -> literal quote
                i += 2
                continue
            in_quotes = not in_quotes
            have_arg = True
            i += 1
            continue
        if c in " \t" and not in_quotes:
            if have_arg:
                args.append("".join(cur))
                cur, have_arg = [], False
            i += 1
            continue
        cur.append(c)
        have_arg = True
        i += 1
    if in_quotes:
        raise CommandSpecError("unbalanced double quote")
    if have_arg:
        args.append("".join(cur))
    return args


def _has_unquoted_shell_ops(cmd: str, windows: bool) -> bool:
    in_double = in_single = False
    prev = ""
    for c in cmd:
        if c == '"' and not in_single and prev != "\\":
            in_double = not in_double
        elif c == "'" and not in_double and not windows:
            in_single = not in_single
        elif not in_double and not in_single:
            if c in _SHELL_CHARS or (c == "(" and prev == "$"):
                return True
        prev = c
    return False


def split_command(cmd: str, *, windows: Optional[bool] = None) -> list[str]:
    windows = IS_WINDOWS if windows is None else windows
    if not cmd.strip():
        raise CommandSpecError("empty command")
    if "\x00" in cmd or "\n" in cmd or "\r" in cmd:
        raise CommandSpecError("control characters in command")
    if _has_unquoted_shell_ops(cmd, windows):
        raise CommandSpecError("shell operators are not supported (no shell is used); use list form or separate commands")
    try:
        argv = _split_windows(cmd) if windows else shlex.split(cmd, posix=True)
    except ValueError as exc:
        raise CommandSpecError(str(exc)) from exc
    if not argv:
        raise CommandSpecError("empty command")
    return argv


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #


_FILE_MODES = {"exists": "file_exists", "absent": "file_absent", "unchanged": "file_unchanged", "json_valid": "json_valid"}
_FILE_LABEL = {"exists": "file", "absent": "absent", "unchanged": "unchanged", "json_valid": "json"}


@dataclass
class NormalizedCheck:
    name: str
    argv: Optional[list[str]] = None
    file: Optional[str] = None
    contains: Optional[str] = None
    timeout: Optional[float] = None
    error: Optional[str] = None
    mode: str = "exists"  # file checks: exists | absent | unchanged | json_valid


def normalize(specs: list[Any]) -> list[NormalizedCheck]:
    out: list[NormalizedCheck] = []
    for i, spec in enumerate(specs):
        name = f"check_{i + 1}"
        try:
            if isinstance(spec, dict):
                spec = VerifyCheck.model_validate(spec)
            if isinstance(spec, VerifyCheck):
                name = spec.name or name
                file_kinds = [(k, getattr(spec, f)) for k, f in _FILE_MODES.items() if getattr(spec, f)]
                if len(file_kinds) > 1 or (file_kinds and spec.cmd is not None):
                    raise CommandSpecError("a check must use exactly one of cmd / file_exists / file_absent / file_unchanged / json_valid")
                if spec.contains is not None and not spec.file_exists:
                    raise CommandSpecError("contains is only valid together with file_exists")
                if file_kinds:
                    mode, path = file_kinds[0]
                    out.append(NormalizedCheck(name=spec.name or f"{_FILE_LABEL[mode]}:{path}", file=path, contains=spec.contains, mode=mode))
                    continue
                if spec.cmd is None:
                    raise CommandSpecError("check needs cmd or file_exists")
                argv = spec.cmd if isinstance(spec.cmd, list) else split_command(spec.cmd)
                out.append(NormalizedCheck(name=spec.name or " ".join(argv)[:80], argv=list(argv), timeout=spec.timeout))
            elif isinstance(spec, list):
                if not spec or not all(isinstance(a, str) for a in spec):
                    raise CommandSpecError("list-form command must be a non-empty list of strings")
                out.append(NormalizedCheck(name=" ".join(spec)[:80], argv=list(spec)))
            elif isinstance(spec, str):
                argv = split_command(spec)
                out.append(NormalizedCheck(name=spec[:80], argv=argv))
            else:
                raise CommandSpecError(f"unsupported verify spec type {type(spec).__name__}")
        except (CommandSpecError, ValueError) as exc:
            out.append(NormalizedCheck(name=name, error=str(exc)))
    return out


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


def kill_tree(pid: int) -> None:
    """Terminate a process and all its descendants. Best effort, never raises."""
    try:
        parent = psutil.Process(pid)
        procs = parent.children(recursive=True) + [parent]
    except psutil.Error:
        procs = []
    for p in procs:
        try:
            p.kill()
        except psutil.Error:
            pass
    if procs:
        psutil.wait_procs(procs, timeout=5)
    if IS_WINDOWS:
        try:
            if psutil.pid_exists(pid):
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    timeout=15,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
        except (OSError, subprocess.SubprocessError):
            pass


def resolve_executable(argv0: str, env: dict[str, str], cwd: str) -> str:
    if os.path.dirname(argv0):
        candidate = argv0 if os.path.isabs(argv0) else os.path.join(cwd, argv0)
        return candidate
    found = shutil.which(argv0, path=env.get("PATH") or env.get("Path"))
    if found is None:
        raise FileNotFoundError(f"executable not found on PATH: {argv0}")
    return found


def _is_cmd_script(exe: str) -> bool:
    """True when `exe` runs through cmd.exe. Normalised first: realpath, then trailing dots/spaces stripped
    (Windows ignores them, so `x.bat.` and `x.bat ` are still batch files)."""
    for candidate in (exe, os.path.realpath(exe)):
        base = os.path.basename(candidate.replace("\\", "/")).rstrip(". ").lower()
        if base.endswith((".cmd", ".bat")):
            return True
    return False


def _track_descendants(pid: int, seen: dict[int, float]) -> None:
    """Remember the process tree under `pid` (pid -> create_time) so survivors can be killed after the parent exits."""
    try:
        for child in psutil.Process(pid).children(recursive=True):
            try:
                seen[child.pid] = child.create_time()
            except psutil.Error:
                continue
    except psutil.Error:
        pass


def _kill_survivors(seen: dict[int, float]) -> None:
    for pid, created in list(seen.items()):
        try:
            p = psutil.Process(pid)
            if abs(p.create_time() - created) < 1.0:  # same process, not a recycled pid
                p.kill()
        except psutil.Error:
            continue


def _tail_text(path: str, max_chars: int) -> str:
    """The end of the output file, read as a bounded tail (never the whole file)."""
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        if size > max_chars * 4:
            fh.seek(size - max_chars * 4)
        raw = fh.read(max_chars * 4)
    return raw.decode("utf-8", errors="replace")


def sandbox_start(session: "sandbox.Session", argv: list[str], env: dict[str, str], out_fd: int
                  ) -> "tuple[sandbox.SandboxedProcess, sandbox.CommandJob]":
    """Start `argv` in the sandbox: Low integrity, suspended, worker job + per-command job, then resumed.
    A separate function only so that the fail-closed tests can simulate a sandbox failure."""
    return session.start(argv, env=env, stdout_fd=out_fd,
                         confine=lambda pid: worker_job.confine_spawned(pid, suspended=False))


def _map_path_into_copy(value: str, worktree: str, copy: str) -> Optional[str]:
    """`value` as the same path inside the copy when it is an absolute path inside the worktree, else None.
    Windows path semantics: case-insensitive, either separator."""
    if "\0" in value or not ntpath.isabs(value) or not ntpath.splitdrive(value)[0]:
        return None
    full = ntpath.normpath(value)
    a, w = ntpath.normcase(full), ntpath.normcase(ntpath.normpath(worktree))
    if a == w:
        return copy
    prefix = w.rstrip("\\") + "\\"
    if a.startswith(prefix):
        return ntpath.join(copy, full[len(prefix):])
    return None


def _map_into_copy(arg: str, worktree: str, copy: str) -> str:
    """An absolute path inside the task worktree - the whole argument, or the value of `--opt=<path>` /
    `-o=<path>` (split at the first '=') - as the same path inside the verification copy."""
    mapped = _map_path_into_copy(arg, worktree, copy)
    if mapped is not None:
        return mapped
    option, sep, value = arg.partition("=")
    if sep and option.startswith("-") and value:
        mapped = _map_path_into_copy(value, worktree, copy)
        if mapped is not None:
            return option + sep + mapped
    return arg


def _path_pattern(path: str) -> str:
    """A regex for `path` in output text: any run of separators ('\\', '/', or escaped '\\\\' as in JSON or a
    repr) between its components."""
    parts = [re.escape(p) for p in re.split(r"[\\/]+", path.rstrip("\\/")) if p]
    return r"[\\/]+".join(parts)


def _map_output(text: str, mapping: list[tuple[str, str]]) -> str:
    """Replace disposable sandbox paths in command output, given (path, replacement) pairs. Longest path
    first, so the copy wins over the run directory containing it; case-insensitive, either separator."""
    pairs = sorted(((p, r) for p, r in mapping if p), key=lambda pr: len(pr[0]), reverse=True)
    if not pairs:
        return text
    pattern = re.compile("|".join(f"(?P<p{i}>{_path_pattern(p)})" for i, (p, _r) in enumerate(pairs)),
                         re.IGNORECASE)
    return pattern.sub(lambda m: pairs[int(m.lastgroup[1:])][1], text)


_NativeResult = TypeVar("_NativeResult")


def _remove_throwaway(path: str) -> None:
    """The per-call sandbox root of a direct caller, removed without following links. What cannot be removed
    is contained residue (logged), not a security finding."""
    from orchestrator.security import verify_tree

    if not IS_WINDOWS:
        sandbox.remove_tree(path)
        return
    try:
        verify_tree.remove_path(path)
    except verify_tree.ResidueRetained as exc:
        log.warning("verification throwaway sandbox retained: %s", exc)


async def _native(session: sandbox.Session, operation: Callable[..., _NativeResult], *args: Any) -> _NativeResult:
    """Join a trusted native operation even through repeated caller cancellation.

    Cancelling an await does not stop the thread behind it. Keep the thread's future shielded and
    consume its result/error before propagating cancellation or releasing any resources.
    Cancellation latency therefore includes the remaining native operation and cleanup.

    The thread is awaited through a plain executor future, never through an asyncio Task: event-loop
    teardown (asyncio.run / Runner.close) cancels every Task of the loop directly, which would end this
    join while the native operation is still running - cleanup would then meet a job whose exit is not
    yet verified and retain the session (runtime reference, run directory) for the process lifetime.
    """
    queued = timing.clock()

    def measured_operation():
        timing.elapsed("native_queue", queued)
        with timing.measure("native_operation"):
            return operation(*args)

    task = asyncio.get_running_loop().run_in_executor(None, contextvars.copy_context().run, measured_operation)
    cancelled = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = exc
            session.cancel_pending_start()
        except Exception:
            break  # Retrieve the thread's exception below, including after cancellation.
    try:
        result = task.result()
    except BaseException:
        if cancelled is not None:
            raise cancelled
        raise
    if cancelled is not None:
        raise cancelled
    return result


@timing.capture_log(log)
async def run_command(argv: list[str], cwd: str, timeout: float, env: dict[str, str], *, strict: bool = False,
                      tmp_dir: Optional[str | os.PathLike[str]] = None,
                      sandbox_root: Optional[str | os.PathLike[str]] = None, writable_cwd: bool = False,
                      data_dir: Optional[str | os.PathLike[str]] = None,
                      protected_files: tuple[str | os.PathLike[str], ...] = ()) -> CheckResult:
    """Run one verify command inside the Low-integrity sandbox (security/sandbox.py, finding N2).

    `strict` (leader-proposed / approved specs): argv[0] must be a bare command name.
    `tmp_dir`: where the output log is written (the server passes `<data dir>/tmp`, owner-only, re-review N8;
    None = the system temp dir); the child only receives an inherited handle to it.
    `sandbox_root`: the per-project sandbox dir (TEMP and tool caches); None = a throw-away dir for this command.
    `writable_cwd`: cwd is an orchestrator task worktree. The command never runs in it: the session copies it
    into a disposable per-command tree (security/verify_tree.py), runs the command there and removes the copy.
    Nothing the command writes reaches the worktree; arguments naming absolute paths inside the worktree are
    mapped to the copy and the copy's path is mapped back in the reported output. Any other cwd is refused.
    `data_dir` / `protected_files`: checked for the no-read-up label before the command (the Verifier always
    passes them; a missing label refuses the command).

    There is no unsandboxed path: when the sandbox cannot be set up the command is not started and the result
    error starts with `sandbox_unavailable:`. When the worktree's `.git` link or agent configuration is not intact
    before the command, or the command changed it, the error starts with `sandbox_tamper:`. Both are security
    errors (`is_security_error`): the executor blocks the task. While the command runs no orchestrator git call
    operates on the worktree (`git.worktree_busy`). Native setup and cleanup run in joined threads;
    cancellation vetoes pending resume but waits for trusted setup/cleanup to settle. The command deadline
    still counts from entry, including setup time; offloading never gives the command a fresh budget."""
    name = " ".join(argv)[:80]
    started = time.monotonic()
    if strict and (any(c in argv[0] for c in "/\\:") or argv[0] != argv[0].strip() or argv[0].endswith(".")):
        return CheckResult(name=name, kind="command", argv=argv, passed=False, error="executable must be a bare command name (no directory component)")
    try:
        exe = resolve_executable(argv[0], env, cwd)
    except FileNotFoundError as exc:
        return CheckResult(name=name, kind="command", argv=argv, passed=False, error=str(exc))
    if IS_WINDOWS and _is_cmd_script(exe):
        # .cmd/.bat run through cmd.exe: refuse metacharacters in arguments (BatBadBut class).
        if any(ch in _CMD_META for a in argv[1:] for ch in a):
            return CheckResult(
                name=name, kind="command", argv=argv, passed=False, error="shell metacharacters in arguments to a .cmd/.bat"
            )
    full = [exe, *argv[1:]]
    throwaway: Optional[str] = None
    if sandbox_root is None:
        throwaway = tempfile.mkdtemp(prefix="orch-sandbox-")
        sandbox_root = throwaway
    class StartupDeadlineExceeded(Exception):
        pass

    def startup_guard() -> None:
        if time.monotonic() >= started + timeout:
            raise StartupDeadlineExceeded()

    session = sandbox.Session(cwd, Path(sandbox_root), writable_cwd=writable_cwd,
                              data_dir=Path(data_dir) if data_dir is not None else None,
                              protected_files=[Path(p) for p in protected_files], start_guard=startup_guard)
    if session.cwd != cwd:
        full = [_map_into_copy(a, cwd, session.cwd) for a in full]
    proc: Optional[sandbox.SandboxedProcess] = None
    job: Optional[sandbox.CommandJob] = None
    out_path: Optional[str] = None
    timed_out = False
    too_much_output = False
    seen: dict[int, float] = {}
    busy = contextlib.ExitStack()
    try:
        if writable_cwd:
            try:
                busy.enter_context(git.worktree_busy(cwd))
            except git.WorktreeBusy as exc:
                return CheckResult(name=name, kind="command", argv=argv, passed=False,
                                   error=str(sandbox.SandboxUnavailable(f"worktree is busy: {exc}")))
        try:
            startup_guard()
            await _native(session, session.prepare)
            startup_guard()
        except StartupDeadlineExceeded:
            return CheckResult(name=name, kind="command", argv=argv, passed=False, timed_out=True,
                               duration_seconds=round(time.monotonic() - started, 3),
                               error=f"timed out after {timeout}s")
        except (sandbox.SandboxUnavailable, sandbox.SandboxTampered) as exc:
            log.error("SECURITY: verification command not run: %s", exc)
            return CheckResult(name=name, kind="command", argv=argv, passed=False, error=str(exc))
        if tmp_dir is not None:
            os.makedirs(tmp_dir, exist_ok=True)
        out_fd, out_path = tempfile.mkstemp(prefix="orch-verify-", suffix=".log", dir=os.fspath(tmp_dir) if tmp_dir is not None else None)
        try:
            # Worker-written code runs here: Low integrity, worker job and per-command job before it runs.
            def start() -> None:
                nonlocal proc, job

                # Adopt even a late result before _native propagates cancellation.
                proc, job = sandbox_start(session, full, env, out_fd)

            await _native(session, start)
        except StartupDeadlineExceeded:
            return CheckResult(name=name, kind="command", argv=argv, passed=False, timed_out=True,
                               duration_seconds=round(time.monotonic() - started, 3),
                               error=f"timed out after {timeout}s")
        except sandbox.SandboxUnavailable as exc:
            log.error("SECURITY: verification command not run: %s", exc)
            return CheckResult(name=name, kind="command", argv=argv, passed=False, error=str(exc))
        except OSError as exc:
            return CheckResult(name=name, kind="command", argv=argv, passed=False, error=f"failed to start: {exc}")
        finally:
            os.close(out_fd)
        deadline = started + timeout
        child_observation_started = timing.clock()
        ticks = 0
        # Bounded poll loop: exits on process exit, deadline or the output cap.
        while proc.poll() is None:
            if time.monotonic() >= deadline:
                timed_out = True
                break
            ticks += 1
            if ticks % 4 == 1:
                await _native(session, _track_descendants, proc.pid, seen)
            try:
                if os.path.getsize(out_path) > MAX_OUTPUT_BYTES:
                    too_much_output = True
                    break
            except OSError:
                pass
            await asyncio.sleep(POLL_SECONDS)
        timing.elapsed("child_observation", child_observation_started)
        # Exit between polls is not evidence that completion met the budget.
        # Conservatively reject a result first observed after the absolute deadline.
        timed_out = timed_out or time.monotonic() >= deadline
        if timed_out or too_much_output:
            if proc.poll() is None:
                await _native(session, kill_tree, proc.pid)
        try:
            await _native(session, proc.wait, 10)
        except subprocess.TimeoutExpired:
            await _native(session, job.terminate)
            await _native(session, proc.wait, 10)
        await _native(session, _kill_survivors, seen)  # grandchildren must not outlive the command
        await _native(session, job.close)  # ... including any the poll missed
        job = None
        tampered = await _native(session, session.finish)
        text = _tail_text(out_path, OUTPUT_LIMIT)
        if session.cwd != cwd:
            # Report worktree paths, not the disposable copy; scratch/cache/run paths become placeholders.
            text = _map_output(text, [(session.cwd, cwd), (str(session.tmp), "<sandbox-tmp>"),
                                      (str(session.cache), "<sandbox-cache>"), (str(session.root), "<sandbox-run>")])
        exit_code = proc.returncode
        error = None
        if timed_out:
            error = f"timed out after {timeout}s"
        elif too_much_output:
            error = f"output exceeded {MAX_OUTPUT_BYTES} bytes; the command was stopped"
        if tampered:
            error = f"{sandbox.TAMPER_CODE}: " + "; ".join(tampered) + (f"; {error}" if error else "")
        return CheckResult(
            name=name,
            kind="command",
            argv=argv,
            passed=(not timed_out and not too_much_output and not tampered and exit_code == 0),
            exit_code=exit_code,
            timed_out=timed_out,
            duration_seconds=round(time.monotonic() - started, 3),
            output=truncate(redact(text), OUTPUT_LIMIT),
            error=error,
        )
    finally:
        import sys

        cleanup_errors = []
        pending_error = sys.exception()
        if isinstance(pending_error, Exception):
            cleanup_errors.append(f"verification did not finish securely: {pending_error}")
        already_finished = session._finished

        @timing.timed("cleanup")
        def cleanup_native() -> None:
            # Every preceding native operation is settled. Keep this entire sequence
            # joined too, so repeated cancellation cannot strand cleanup halfway through.
            cleanups = []
            if pending_error is not None:
                cleanups.extend((
                    ("process tree", lambda: kill_tree(proc.pid) if proc is not None and proc.poll() is None else None),
                    ("descendants", lambda: _kill_survivors(seen)),
                ))
            cleanups.extend((
                ("command job", job.close if job is not None else None),
                ("process handle", proc.close if proc is not None else None),
                ("sandbox session", session.finish),
                ("throwaway directory", lambda: _remove_throwaway(throwaway) if throwaway else None),
                ("output log", lambda: os.unlink(out_path) if out_path is not None else None),
            ))
            for label, cleanup in cleanups:
                if cleanup is not None:
                    try:
                        findings = cleanup()
                        if label == "sandbox session" and findings and not already_finished:
                            cleanup_errors.extend(findings)
                    except Exception as exc:
                        cleanup_errors.append(f"cannot clean up {label}: {exc}")
                        if label == "command job":
                            git.quarantine_worktree(cwd, f"verification job exit unverified: {exc}")

        try:
            await _native(session, cleanup_native)
        finally:
            command_job = session._command_job
            if (command_job is not None and not command_job._drained
                    or getattr(session, "_unverified_startup_process", None) is not None):
                git.quarantine_worktree(cwd, "verification process or command job exit could not be verified")
            busy.close()
        if cleanup_errors:
            # Override a pending success or ordinary failure with a security result.
            # Cancellation still propagates after the reservation has been released.
            if not isinstance(pending_error, asyncio.CancelledError):
                return CheckResult(name=name, kind="command", argv=argv, passed=False,
                                   error=f"{sandbox.TAMPER_CODE}: " + "; ".join(cleanup_errors))


MAX_CHECK_BYTES = 32 * 1024 * 1024


def _check_rel(cwd: str, rel: str) -> str:
    """The check's path as a relative posix path inside `cwd` (an absolute path must lie inside it). Raises
    PathPolicyError. Files are then accessed only through the no-follow helpers (N2 re-review S11)."""
    if os.path.isabs(rel) or (len(rel) > 1 and rel[1] == ":"):
        inside = resolve_inside(rel, [cwd], base=cwd)
        rel = os.path.relpath(inside, os.path.realpath(cwd))
    path_policy._rel_parts(rel)  # validation only: relative, no '..', no ADS / device syntax
    return Path(rel).as_posix()


def _fail(name: str, rel: str, error: str) -> CheckResult:
    return CheckResult(name=name, kind="file", path=rel, passed=False, error=error)


def check_file(cwd: str, rel: str, contains: Optional[str]) -> CheckResult:
    name = f"file:{rel}"
    try:
        posix = _check_rel(cwd, rel)
        if contains is None:
            if path_policy.file_size_inside(cwd, posix) is None:
                return _fail(name, rel, "file does not exist (or is not a regular, unlinked file)")
            return CheckResult(name=name, kind="file", path=rel, passed=True)
        data = path_policy.read_inside(cwd, posix, max_bytes=MAX_CHECK_BYTES)
    except (FileNotFoundError, IsADirectoryError):
        return _fail(name, rel, "file does not exist")
    except (PathPolicyError, OSError) as exc:
        return _fail(name, rel, str(exc))
    if contains not in data.decode("utf-8", errors="replace"):
        return _fail(name, rel, "expected text not found")
    return CheckResult(name=name, kind="file", path=rel, passed=True)


def check_file_absent(cwd: str, rel: str) -> CheckResult:
    name = f"absent:{rel}"
    try:
        posix = _check_rel(cwd, rel)
        target = path_policy._walk_parents(os.path.realpath(cwd), path_policy._rel_parts(posix))
    except FileNotFoundError:
        return CheckResult(name=name, kind="file", path=rel, passed=True)  # a parent is missing
    except (PathPolicyError, OSError) as exc:
        return _fail(name, rel, str(exc))
    if os.path.lexists(target):
        return _fail(name, rel, "path exists but must be absent")
    return CheckResult(name=name, kind="file", path=rel, passed=True)


def check_json_valid(cwd: str, rel: str) -> CheckResult:
    name = f"json:{rel}"
    try:
        data = path_policy.read_inside(cwd, _check_rel(cwd, rel), max_bytes=MAX_CHECK_BYTES)
    except (FileNotFoundError, IsADirectoryError):
        return _fail(name, rel, "file does not exist")
    except (PathPolicyError, OSError) as exc:
        return _fail(name, rel, str(exc))
    try:
        json.loads(data.decode("utf-8-sig"))
    except ValueError as exc:
        return _fail(name, rel, f"not valid JSON: {str(exc)[:200]}")
    return CheckResult(name=name, kind="file", path=rel, passed=True)


def check_file_unchanged(cwd: str, rel: str, base: Optional[str]) -> CheckResult:
    """The worktree file equals its version at the task's base commit (HEAD when the task has none).
    Implemented with read-only git plumbing (trusted git dir, see gitops.git); a file absent in both places counts
    as unchanged. A link (junction/symlink/hardlink) where the file should be counts as changed."""
    name = f"unchanged:{rel}"
    try:
        posix = _check_rel(cwd, rel)
        target = path_policy._walk_parents(os.path.realpath(cwd), path_policy._rel_parts(posix))
        present = os.path.lexists(target)
    except FileNotFoundError:
        present = False
    except (PathPolicyError, OSError) as exc:
        return _fail(name, rel, str(exc))
    ref = base or "HEAD"
    if ref.startswith("-"):
        return _fail(name, rel, "invalid base ref")
    if not git.is_repo(cwd):
        return _fail(name, rel, "not a git repository: cannot compare with the base commit")
    try:
        in_base = git.run_git(["cat-file", "-e", f"{ref}:{posix}"], cwd, check=False, timeout=30).returncode == 0
        regular = present and path_policy.file_size_inside(cwd, posix) is not None
        if not in_base:
            ok = not present
            err = None if ok else "file did not exist at the base commit but exists now"
        elif not present:
            ok, err = False, "file existed at the base commit but is missing now"
        elif not regular:
            ok, err = False, "the path is no longer a regular file (link, hardlink or directory)"
        else:
            proc = git.run_git(["diff", "--quiet", "--no-ext-diff", ref, "--", posix], cwd, check=False, timeout=30)
            ok = proc.returncode == 0
            err = None if ok else "file differs from the base commit"
            if proc.returncode not in (0, 1):
                ok, err = False, f"git diff failed: {proc.stderr.strip()[:200]}"
    except git.GitError as exc:
        return _fail(name, rel, str(exc)[:300])
    return CheckResult(name=name, kind="file", path=rel, passed=ok, error=err)


def is_security_error(r: CheckResult) -> bool:
    """`sandbox_unavailable:` / `sandbox_tamper:`: never a repair cycle, never auto-approvable."""
    return sandbox.is_security_error(r.error)


def is_sandbox_unavailable(r: CheckResult) -> bool:
    return bool(r.error and r.error.startswith(sandbox.ERROR_CODE))


class Verifier:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx

    def is_task_worktree(self, cwd: str) -> bool:
        """True for an orchestrator task worktree (`<worktrees dir>/<task id>`): the only cwd verification
        commands accept. It is copied into a disposable tree for each command and never written by it. Anything
        else - an in_place project folder (the user's own directory) - is refused."""
        try:
            path = Path(os.path.realpath(cwd))
            root = Path(os.path.realpath(self.ctx.paths.worktrees_dir))
        except (OSError, ValueError):
            return False
        return (os.path.normcase(str(path.parent)) == os.path.normcase(str(root))
                and not path.name.startswith("_") and path.is_dir())

    async def run_checks(
        self, specs: list[Any], cwd: str, *, base_ref: Optional[str] = None, origin: str = "user",
        project_id: Optional[str] = None,
    ) -> tuple[str, list[CheckResult]]:
        policies = await self.ctx.policies()
        default_timeout = float(policies.limit("verify_timeout_seconds"))
        env = child_env(policies.child_env_allowlist)
        checks = normalize(specs)
        if not checks:
            return "no_checks", []
        results: list[CheckResult] = []
        if any(c.argv is not None and not c.error for c in checks):
            from orchestrator.security import verify_tree

            # Once per process: bounded removal of run directories a crash or earlier residue left behind.
            await asyncio.to_thread(verify_tree.sweep_all_once, self.ctx.paths.sandbox_dir)
            if sandbox.retained_sessions():
                # Sessions kept because a job exit could not be verified: retry the verification, never skip it.
                await asyncio.to_thread(sandbox.settle_retained_sessions)
        for c in checks:  # bounded by the number of specs
            if c.error:
                results.append(CheckResult(name=c.name, kind="command", passed=False, error=c.error))
            elif c.file is not None:
                if c.mode == "absent":
                    r = check_file_absent(cwd, c.file)
                elif c.mode == "unchanged":
                    r = await asyncio.to_thread(check_file_unchanged, cwd, c.file, base_ref)
                elif c.mode == "json_valid":
                    r = check_json_valid(cwd, c.file)
                else:
                    r = check_file(cwd, c.file, c.contains)
                r.name = c.name
                results.append(r)
            else:
                assert c.argv is not None
                paths = self.ctx.paths
                r = await run_command(c.argv, cwd, c.timeout or default_timeout, env, strict=origin != "user",
                                      tmp_dir=paths.tmp_dir,
                                      sandbox_root=paths.sandbox_root(project_id),
                                      writable_cwd=self.is_task_worktree(cwd),
                                      data_dir=paths.data_dir, protected_files=(paths.db_path, paths.token_path))
                r.name = c.name
                results.append(r)
                if is_security_error(r):
                    break  # nothing more runs in a worktree that failed a security check
        if any(c.error for c in checks) or any(is_security_error(r) for r in results):
            status = "error"
        else:
            status = "passed" if all(r.passed for r in results) else "failed"
        return status, results

    async def verify(self, task: Task, cwd: str, *, specs: list[Any], run_id: Optional[str] = None, origin: str = "user") -> Verification:
        started = now_iso()
        status, results = await self.run_checks(specs, cwd, base_ref=task.base_commit, origin=origin,
                                                project_id=task.project_id)
        vid = new_id("vrf")

        def fn(tx: Tx) -> Verification:
            records.verification_insert(
                tx,
                {
                    "id": vid,
                    "task_id": task.id,
                    "run_id": run_id,
                    "status": status,
                    "origin": origin,
                    "checks": [r.model_dump(mode="json") for r in results],
                    "started_at": started,
                    "finished_at": now_iso(),
                },
            )
            self.ctx.bus.append(
                tx,
                ev.VERIFICATION_RESULT,
                project_id=task.project_id,
                task_id=task.id,
                run_id=run_id,
                payload={
                    "verification_id": vid,
                    "status": status,
                    "origin": origin,
                    "checks": [
                        {"name": r.name, "passed": r.passed, "exit_code": r.exit_code, "timed_out": r.timed_out, "error": r.error}
                        for r in results
                    ],
                },
            )
            v = Verification.from_row(tx.one("SELECT * FROM verifications WHERE id = ?", (vid,)))
            assert v is not None
            return v

        return await self.ctx.db.write(fn)


__all__ = ["Verifier", "split_command", "normalize", "run_command", "kill_tree", "CommandSpecError"]
