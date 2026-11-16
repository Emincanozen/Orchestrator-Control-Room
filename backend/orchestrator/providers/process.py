"""Safe async subprocess layer shared by all CLI adapters.

Rules (ADR-005, ARCHITECTURE section 7/12):
  * argument arrays only, never ``shell=True``, ``cmd.exe`` is never involved;
  * the prompt travels on stdin;
  * the child gets an allow-listed environment plus ``AI_ORCHESTRATOR_DEPTH``;
  * every child runs in its own process group and is terminated as a tree.

The module also hosts :class:`CliAdapterBase`, the shared ``execute()`` loop, so
that the three adapters only supply command building, a pure line parser and
error patterns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import psutil
import yaml

from orchestrator.security import worker_job

from .base import (
    AgentEvent,
    AgentEventKind,
    ErrorCategory,
    ExecutionRequest,
    ExecutionResult,
    ProviderAdapter,
    RecursionGuardError,
    UnsupportedOperation,
)

log = logging.getLogger(__name__)

DEPTH_ENV = "AI_ORCHESTRATOR_DEPTH"
IS_WINDOWS = sys.platform == "win32"

# Used only when config/policies.yaml cannot be read.
DEFAULT_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE",
    "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "USERNAME", "COMPUTERNAME", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "LANG", "TERM",
    "CODEX_HOME", "CLAUDE_CONFIG_DIR",
)

TAIL_MAX_CHARS = 32_000
LINE_LIMIT = 32 * 1024 * 1024


class ExecutableResolutionError(RuntimeError):
    """The real executable behind a CLI name could not be determined safely."""


class InvalidRequestError(UnsupportedOperation):
    """The request itself is unsafe or malformed (bad session id, cwd inside the data dir, ...).

    Subclasses UnsupportedOperation so every existing handler maps it to ErrorCategory.INVALID_REQUEST."""


# Session ids from earlier runs (security finding #13): a conservative charset, bounded, never option-like.
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._:][A-Za-z0-9._:-]{0,127}$")


def validate_session_id(session_id: str | None) -> str | None:
    """Return the id unchanged, or raise InvalidRequestError before it can reach a command line."""
    if session_id is None:
        return None
    if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
        raise InvalidRequestError(
            "resume session id rejected: expected 1-128 characters of [A-Za-z0-9._:-], not starting with '-'"
        )
    return session_id


def _norm_real(p: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.normpath(os.path.realpath(os.fspath(p))))


def overlaps(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    """True when the resolved paths are equal or one contains the other."""
    na, nb = _norm_real(a), _norm_real(b)
    if na == nb:
        return True
    return na.startswith(nb.rstrip("\\/") + os.sep) or nb.startswith(na.rstrip("\\/") + os.sep)


def check_run_paths(cwd: str, extra_dirs: Iterable[str], protected: Iterable[str | os.PathLike[str]]) -> None:
    """Refuse a provider run whose cwd or any extra dir is, is inside, or contains a protected directory
    (the orchestrator data dir: API token + database; the orchestrator's own installation: code, config,
    venv). Security finding #1, re-review N3. A project row registered before N3 that points at such a
    directory is refused here, at run time."""
    prot = [p for p in protected if p]
    for label, path in [("working directory", cwd), *(("extra directory", d) for d in extra_dirs)]:
        if not path or "\x00" in str(path):
            raise InvalidRequestError(f"{label} is empty or contains a NUL byte")
        for d in prot:
            if overlaps(path, d):
                raise InvalidRequestError(
                    f"refused: the {label} {path} overlaps the protected orchestrator directory {d} (the data "
                    "directory holds the API token and the database; the installation holds the orchestrator's "
                    "code, configuration and Python environment). Register a project folder outside them."
                )


# --------------------------------------------------------------------------- environment


def repo_config_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "config"


def load_env_allowlist(policies_path: Path | None = None) -> list[str]:
    path = policies_path or (repo_config_dir() / "policies.yaml")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        names = data["security"]["child_env_allowlist"]
        if isinstance(names, list) and all(isinstance(n, str) for n in names) and names:
            return list(names)
    except Exception as exc:  # noqa: BLE001 - fall back to the built-in list
        log.warning("could not read child_env_allowlist from %s (%s); using built-in list", path, exc)
    return list(DEFAULT_ENV_ALLOWLIST)


def current_depth(environ: dict[str, str] | None = None) -> int:
    raw = (environ if environ is not None else os.environ).get(DEPTH_ENV, "0")
    try:
        return max(0, int(raw))
    except ValueError:
        return 1  # unparsable value: be conservative and treat as nested


def check_recursion_guard(environ: dict[str, str] | None = None) -> None:
    if current_depth(environ) >= 1:
        raise RecursionGuardError(
            f"{DEPTH_ENV}>=1: refusing to start an orchestrator-launched agent from inside another one"
        )


def build_child_env(
    allowlist: Iterable[str] | None = None,
    *,
    base_env: dict[str, str] | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Allow-listed copy of the environment plus AI_ORCHESTRATOR_DEPTH=depth+1."""
    source = base_env if base_env is not None else dict(os.environ)
    allowed = {n.upper() for n in (allowlist if allowlist is not None else load_env_allowlist())}
    # ORCHESTRATOR_* (data dir, token file, ...) never reaches a provider, even if someone allowlists it.
    env = {k: v for k, v in source.items() if k.upper() in allowed and not k.upper().startswith("ORCHESTRATOR_")}
    if extra:
        env.update(extra)
    env[DEPTH_ENV] = str(current_depth(source) + 1)
    return env


# --------------------------------------------------------------------------- executable resolution

_SHIM_TARGET = re.compile(r'%dp0%[\\/]+([^"%\r\n]+?\.(?:exe|js))"', re.IGNORECASE)
_resolve_cache: dict[tuple[str, str | None], ResolvedExecutable] = {}


@dataclass(frozen=True)
class ResolvedExecutable:
    argv_prefix: tuple[str, ...]  # e.g. (exe,) or (node, entry.js)
    path: str  # the file reported to users (exe or JS entry)
    via: str  # human description of how it was resolved


def clear_resolve_cache() -> None:
    _resolve_cache.clear()


def _parse_npm_shim(shim: Path) -> ResolvedExecutable:
    try:
        text = shim.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ExecutableResolutionError(f"cannot read shim {shim}: {exc}") from exc
    matches = _SHIM_TARGET.findall(text)
    if not matches:
        raise ExecutableResolutionError(
            f"{shim} is a .cmd/.bat shim whose target could not be determined; refusing to run it through cmd.exe"
        )
    rel = matches[-1]  # the last quoted %dp0% target is the program being launched
    target = (shim.parent / rel.replace("\\", os.sep)).resolve()
    if not target.is_file():
        raise ExecutableResolutionError(f"shim {shim} points to {target}, which does not exist")
    if target.suffix.lower() == ".exe":
        return ResolvedExecutable((str(target),), str(target), f"npm shim {shim.name} -> native exe")
    node = shim.parent / "node.exe"  # npm shims prefer a node.exe sitting next to them
    node_path = str(node) if node.is_file() else shutil.which("node")
    if not node_path:
        raise ExecutableResolutionError(f"shim {shim} needs node but node was not found on PATH")
    return ResolvedExecutable((node_path, str(target)), str(target), f"npm shim {shim.name} -> node + {target.name}")


def resolve_executable(
    name: str, *, search_path: str | None = None, extra_candidates: Iterable[str] = ()
) -> ResolvedExecutable:
    """Resolve a CLI name to a directly launchable program. Never returns a cmd/bat/ps1 path."""
    key = (name, search_path)
    cached = _resolve_cache.get(key)
    if cached is not None:
        return cached

    found: Path | None = None
    candidates: list[str] = []
    if IS_WINDOWS:
        for ext in (".exe", ".cmd", ".bat"):
            hit = shutil.which(name + ext, path=search_path)
            if hit:
                candidates.append(hit)
    else:
        hit = shutil.which(name, path=search_path)
        if hit:
            candidates.append(hit)
    candidates.extend(c for c in extra_candidates if c)
    for cand in candidates:
        if Path(cand).is_file():
            found = Path(cand)
            break
    if found is None:
        raise ExecutableResolutionError(f"executable {name!r} not found on PATH")

    suffix = found.suffix.lower()
    if suffix in (".cmd", ".bat"):
        resolved = _parse_npm_shim(found)
    elif suffix == ".ps1":
        raise ExecutableResolutionError(f"{found} is a PowerShell script; not executable safely")
    else:
        resolved = ResolvedExecutable((str(found),), str(found), "native executable")
    _resolve_cache[key] = resolved
    return resolved


# --------------------------------------------------------------------------- process handle


class ProcessHandle(Protocol):
    """What adapters need from a running child (real or fake)."""

    pid: int | None
    returncode: int | None
    timed_out: bool
    killed: bool

    def stream(self) -> AsyncIterator[tuple[str, str]]: ...

    @property
    def stdout_tail(self) -> str: ...

    @property
    def stderr_tail(self) -> str: ...

    async def wait(self) -> int: ...

    async def terminate_tree(self) -> bool: ...


Spawner = Callable[..., Awaitable[ProcessHandle]]


class _Tail:
    def __init__(self, max_chars: int = TAIL_MAX_CHARS) -> None:
        self._lines: deque[str] = deque()
        self._chars = 0
        self._max = max_chars

    def add(self, line: str) -> None:
        self._lines.append(line)
        self._chars += len(line) + 1
        while self._chars > self._max and len(self._lines) > 1:
            self._chars -= len(self._lines.popleft()) + 1

    def text(self) -> str:
        return "\n".join(self._lines)


def _kill_tree_sync(pid: int, grace: float = 3.0) -> bool:
    """psutil children -> terminate -> kill, then taskkill /T /F as a Windows fallback."""
    killed_any = False
    if not IS_WINDOWS:
        # The process group survives its leader. This remains best effort on
        # POSIX (a hostile child can create a different session).
        try:
            os.killpg(pid, signal.SIGKILL)
            killed_any = True
        except ProcessLookupError:
            pass
        except OSError:
            pass
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return killed_any
    try:
        procs = parent.children(recursive=True) + [parent]
    except psutil.Error:
        procs = [parent]
    for p in procs:
        try:
            p.terminate()
            killed_any = True
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(procs, timeout=grace)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(alive, timeout=grace)
    if alive and IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    return killed_any


def _confine_or_kill(proc: asyncio.subprocess.Process, job: worker_job.SpawnJob | None = None) -> None:
    """Assign a just-created (suspended on Windows) child to the worker job, then resume it (finding #1).

    Synchronous on purpose: no await between creation and assignment, so nothing can cancel us in between.
    If the child cannot be resumed it is killed and OSError propagates to the caller's spawn-failure path."""
    try:
        if IS_WINDOWS:
            if job is None:
                raise worker_job.ContainmentError("missing mandatory per-spawn lifetime job")
            worker_job.confine_spawned(proc.pid, suspended=False)
            job.assign(proc.pid)
            worker_job.resume_spawned(proc.pid)
        else:
            worker_job.confine_spawned(proc.pid, suspended=False)
    except OSError:
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
        raise


async def _drain_job(job: worker_job.SpawnJob) -> bool:
    """Do not cancel native drain or close a handle without an empty-job proof."""
    operation = asyncio.create_task(asyncio.to_thread(job.close))
    deadline = asyncio.get_running_loop().time() + worker_job.DRAIN_TIMEOUT + 2
    cancelled = None
    while not operation.done():
        try:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError
            await asyncio.wait_for(asyncio.shield(operation), remaining)
        except TimeoutError as exc:
            raise worker_job.ContainmentError("per-spawn job drain is still pending") from exc
        except asyncio.CancelledError as exc:
            cancelled = exc  # do not reset the absolute deadline or interrupt native ownership
        except Exception:  # noqa: BLE001 - retrieve the native error below; it outranks cancellation
            break
    result = operation.result()
    if cancelled is not None:
        raise cancelled
    return result


async def _parent_exit(proc: asyncio.subprocess.Process) -> int:
    # asyncio Process.wait can itself depend on pipe EOF. returncode is set by
    # the native process-exit notification independently of inherited pipes.
    while proc.returncode is None:
        await asyncio.sleep(0.025)
    return proc.returncode


_pending_spawns: set[asyncio.Task] = set()
_late_reapers: set[asyncio.Task] = set()
_unreaped_processes: dict[int, asyncio.subprocess.Process] = {}


async def _abort_spawn(proc: asyncio.subprocess.Process, job: worker_job.SpawnJob | None) -> None:
    """Reap a cancelled, still-suspended spawn without ever running user code."""
    _unreaped_processes[proc.pid] = proc
    if IS_WINDOWS and job is not None:
        try:
            worker_job.confine_spawned(proc.pid, suspended=False)
            job.assign(proc.pid)
        except OSError:
            # It is still suspended. Reap it directly even if job assignment
            # failed; unknown termination is escalated below, never ignored.
            pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    except OSError as exc:
        raise worker_job.ContainmentError(f"cannot kill unreturned suspended spawn {proc.pid}: {exc}") from exc
    if job is not None:
        await _drain_job(job)
    try:
        await asyncio.wait_for(proc.communicate(), worker_job.DRAIN_TIMEOUT + 2)
    except TimeoutError as exc:
        raise worker_job.ContainmentError(f"cannot reap cancelled spawn {proc.pid}") from exc
    except OSError as exc:
        raise worker_job.ContainmentError(f"cancelled spawn {proc.pid} reap failed: {exc}") from exc
    _unreaped_processes.pop(proc.pid, None)


def _reap_when_created(task: asyncio.Task, job: worker_job.SpawnJob | None) -> None:
    async def reap() -> None:
        try:
            try:
                proc = await asyncio.shield(task)
            except (OSError, ValueError):
                if job is not None:
                    job.end_spawn()
                    await _drain_job(job)
                return
            if job is not None:
                job.end_spawn()
            await _abort_spawn(proc, job)
        except BaseException:
            # The original caller already received ContainmentError and must
            # quarantine. Keep native jobs/process handles available for retry.
            log.critical("SECURITY: late cancelled spawn cleanup failed", exc_info=True)
        finally:
            _pending_spawns.discard(task)
    reaper = asyncio.create_task(reap())
    _late_reapers.add(reaper)
    reaper.add_done_callback(_late_reapers.discard)


async def _create_child(argv: list[str], job: worker_job.SpawnJob | None, **kwargs) -> asyncio.subprocess.Process:
    if job is not None:
        job.begin_spawn()
    task = asyncio.create_task(asyncio.create_subprocess_exec(*argv, **kwargs))
    _pending_spawns.add(task)
    try:
        proc = await asyncio.shield(task)
    except asyncio.CancelledError as cancellation:
        try:
            proc = await asyncio.wait_for(asyncio.shield(task), worker_job.DRAIN_TIMEOUT + 2)
        except (TimeoutError, asyncio.CancelledError) as exc:
            _reap_when_created(task, job)
            raise worker_job.ContainmentError("cancelled spawn creation is unresolved; cleanup retained") from exc
        except (OSError, ValueError):
            if job is not None:
                job.end_spawn()
                await _drain_job(job)
            _pending_spawns.discard(task)
            raise cancellation
        if job is not None:
            job.end_spawn()
        _pending_spawns.discard(task)
        await _abort_spawn(proc, job)
        raise cancellation
    except BaseException:
        if job is not None:
            job.end_spawn()
        _pending_spawns.discard(task)
        raise
    if job is not None:
        job.end_spawn()
    _pending_spawns.discard(task)
    return proc


class ManagedProcess:
    """A real child process with line streaming, bounded tails, timeout and tree kill."""

    def __init__(self, proc: asyncio.subprocess.Process, timeout: float | None,
                 job: worker_job.SpawnJob | None = None) -> None:
        if IS_WINDOWS and job is None:
            raise worker_job.ContainmentError("managed Windows process has no lifetime job")
        self._proc = proc
        self._job = job
        self._containment_error: worker_job.ContainmentError | None = None
        self._posix_cleaned = False
        self._stdin_writer: asyncio.Task[None] | None = None
        self.pid: int | None = proc.pid
        self.returncode: int | None = None
        self.timed_out = False
        self.killed = False
        self._out = _Tail()
        self._err = _Tail()
        self._queue: asyncio.Queue[tuple[str, str] | None | worker_job.ContainmentError] = asyncio.Queue()
        self._readers = [
            asyncio.create_task(self._pump(proc.stdout, "stdout", self._out)),
            asyncio.create_task(self._pump(proc.stderr, "stderr", self._err)),
        ]
        self._watchdog: asyncio.Task[None] | None = None
        if timeout and timeout > 0:
            self._watchdog = asyncio.create_task(self._timeout_after(timeout))
        self._lifetime = asyncio.create_task(self._monitor_lifetime())

    async def _monitor_lifetime(self) -> None:
        try:
            await _parent_exit(self._proc)
            if self._job is not None:
                await _drain_job(self._job)
            elif self.pid is not None and not self._posix_cleaned:
                await asyncio.to_thread(_kill_tree_sync, self.pid)
                self._posix_cleaned = True
        except worker_job.ContainmentError as exc:
            self._containment_error = exc
            await self._queue.put(exc)  # unblock stream even if a child holds its pipes

    @classmethod
    async def spawn(
        cls,
        argv: list[str],
        *,
        cwd: str,
        env: dict[str, str],
        stdin_text: str | None = None,
        timeout: float | None = None,
    ) -> ManagedProcess:
        kwargs: dict[str, Any] = {}
        job = worker_job.SpawnJob() if IS_WINDOWS else None
        if IS_WINDOWS:
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW | worker_job.spawn_creationflags()
            )
        else:
            kwargs["start_new_session"] = True
        try:
            proc = await _create_child(
                argv, job, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=LINE_LIMIT, **kwargs)
        except worker_job.ContainmentError:
            if job is not None:
                try:
                    await _drain_job(job)
                except asyncio.CancelledError:
                    pass  # preserve the original unresolved-spawn containment error
            raise
        except BaseException:
            if job is not None:
                await _drain_job(job)
            raise
        try:
            _confine_or_kill(proc, job)
            mp = cls(proc, timeout, job)
        except BaseException:
            await _abort_spawn(proc, job)
            raise
        mp._stdin_writer = asyncio.create_task(mp._write_stdin(stdin_text))
        return mp

    async def _write_stdin(self, text: str | None) -> None:
        w = self._proc.stdin
        if w is None:
            return
        try:
            if text:
                w.write(text.encode("utf-8"))
                await w.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass

    async def _pump(self, reader: asyncio.StreamReader | None, kind: str, tail: _Tail) -> None:
        try:
            if reader is not None:
                while True:
                    try:
                        raw = await reader.readline()
                    except ValueError:  # line longer than LINE_LIMIT: drop the chunk, keep going
                        await reader.read(65536)
                        continue
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    tail.add(line)
                    await self._queue.put((kind, line))
        finally:
            await self._queue.put(None)

    async def _timeout_after(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        if self._proc.returncode is None:
            self.timed_out = True
            try:
                await self.terminate_tree()
            except worker_job.ContainmentError as exc:
                self._containment_error = exc
                await self._queue.put(exc)

    async def stream(self) -> AsyncIterator[tuple[str, str]]:  # type: ignore[override]
        finished = 0
        try:
            while finished < 2:
                item = await self._queue.get()
                if isinstance(item, worker_job.ContainmentError):
                    raise item
                if item is None:
                    finished += 1
                    continue
                yield item
            await self.wait()
        finally:
            await self.terminate_tree()

    @property
    def stdout_tail(self) -> str:
        return self._out.text()

    @property
    def stderr_tail(self) -> str:
        return self._err.text()

    async def wait(self) -> int:
        await asyncio.shield(self._lifetime)
        if self._containment_error is not None:
            raise self._containment_error
        try:
            tasks = [*self._readers]
            if self._stdin_writer is not None:
                tasks.append(self._stdin_writer)
            await asyncio.wait_for(asyncio.gather(*tasks), worker_job.DRAIN_TIMEOUT + 2)
            rc = await asyncio.wait_for(self._proc.wait(), worker_job.DRAIN_TIMEOUT + 2)
        except TimeoutError as exc:
            raise worker_job.ContainmentError("worker pipes did not finish after lifetime drain") from exc
        self.returncode = rc
        if self._watchdog and not self._watchdog.done() and asyncio.current_task() is not self._watchdog:
            self._watchdog.cancel()
        return rc

    async def terminate_tree(self) -> bool:
        if self._job is not None:
            was_running = self._proc.returncode is None
            result = await _drain_job(self._job)
            self.killed |= result and was_running
            return result
        if self._posix_cleaned:
            return False
        if self.pid is None or self._proc.returncode is not None and not self._tree_alive():
            if self.pid is None:
                return False
        self.killed = True
        result = await asyncio.to_thread(_kill_tree_sync, self.pid)
        self._posix_cleaned = True
        return result

    def _tree_alive(self) -> bool:
        try:
            return psutil.Process(self.pid).is_running() if self.pid else False
        except psutil.Error:
            return False


async def spawn_process(
    argv: list[str], *, cwd: str, env: dict[str, str], stdin_text: str | None = None, timeout: float | None = None
) -> ProcessHandle:
    return await ManagedProcess.spawn(argv, cwd=cwd, env=env, stdin_text=stdin_text, timeout=timeout)


async def run_capture(
    argv: list[str], *, env: dict[str, str] | None = None, timeout: float = 30.0, cwd: str | None = None
) -> tuple[int | None, str, str]:
    """Run a short helper with stdin closed and the same descendant lifetime boundary.

    Ordinary spawn failure/timeout returns exit_code=None. ContainmentError is
    fatal and propagates so the caller cannot mistake unknown drain for failure
    of an optional detection command.
    """
    job = worker_job.SpawnJob() if IS_WINDOWS else None
    proc = None
    reader = monitor = None
    try:
        kwargs: dict[str, Any] = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW | worker_job.spawn_creationflags()
            )
        else:
            kwargs["start_new_session"] = True
        proc = await _create_child(
            argv, job, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kwargs,
        )
        try:
            _confine_or_kill(proc, job)
        except BaseException:
            await _abort_spawn(proc, job)
            raise
        async def lifetime() -> None:
            await _parent_exit(proc)
            if job is not None:
                await _drain_job(job)
            else:
                await asyncio.to_thread(_kill_tree_sync, proc.pid)

        reader = asyncio.create_task(proc.communicate())
        monitor = asyncio.create_task(lifetime())
        try:
            output, _ = await asyncio.wait_for(asyncio.gather(reader, monitor), timeout=timeout)
        except TimeoutError:
            return None, "", "timed out"
        out, err = output
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    except worker_job.ContainmentError:
        raise
    except (OSError, ValueError) as exc:
        return None, "", f"spawn failed: {exc}"
    finally:
        try:
            if job is not None:
                await _drain_job(job)
            elif proc is not None:
                await asyncio.to_thread(_kill_tree_sync, proc.pid)
        finally:
            tasks = [task for task in (reader, monitor) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                outcomes = await asyncio.gather(*tasks, return_exceptions=True)
                for outcome in outcomes:
                    if isinstance(outcome, worker_job.ContainmentError):
                        raise outcome


# --------------------------------------------------------------------------- parse state + helpers


@dataclass
class ParseState:
    """Accumulator that pure line parsers update; the base class reads it to build RESULT."""

    session_id: str | None = None
    model: str | None = None
    texts: list[str] = field(default_factory=list)
    final_text: str | None = None  # authoritative final message when the provider sends one
    usage: dict[str, Any] = field(default_factory=dict)
    success: bool | None = None  # None = provider never reported an outcome
    error_messages: list[str] = field(default_factory=list)
    structured_output: Any = None
    rate_limit: dict[str, Any] | None = None
    permission_denials: int = 0
    seen_steps: set[Any] = field(default_factory=set)
    extra: dict[str, Any] = field(default_factory=dict)

    def text(self) -> str:
        return self.final_text if self.final_text is not None else "".join(self.texts)


def loads_object(line: str) -> dict[str, Any] | None:
    """Parse one stream line; None for blank/non-JSON/non-object lines."""
    s = line.strip()
    if not s or s[0] != "{":
        return None
    try:
        obj = json.loads(s)
    except (ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


def raw_event(line: str, note: str = "unparsed") -> AgentEvent:
    return AgentEvent(kind=AgentEventKind.RAW, data={"line": line[:2000], "note": note})


def match_any(text: str, patterns: Iterable[str]) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


COMMON_NETWORK = (r"econnrefused", r"econnreset", r"enotfound", r"etimedout", r"getaddrinfo", r"socket hang up",
                  r"network (error|is unreachable)", r"fetch failed", r"connection (error|reset|refused|closed)",
                  r"dns (error|failure)", r"tls handshake", r"unable to connect")


def classify_by_table(
    table: list[tuple[ErrorCategory, tuple[str, ...]]], exit_code: int | None, text: str
) -> ErrorCategory:
    """First matching row wins. Without a pattern match a non-trivial exit code means CRASHED, else UNKNOWN."""
    for category, patterns in table:
        if match_any(text, patterns):
            return category
    if exit_code is not None and exit_code not in (0, 1, 2):
        return ErrorCategory.CRASHED
    return ErrorCategory.UNKNOWN


def first_str(d: dict[str, Any], keys: Iterable[str]) -> str | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v:
            return v
    return None


# --------------------------------------------------------------------------- adapter base


@dataclass
class CommandSpec:
    args: list[str]  # arguments after the executable prefix
    stdin_text: str | None
    cleanup: list[Path] = field(default_factory=list)  # temp files to delete after the run
    environment: dict[str, str] | None = None  # optional per-command sanitized runtime snapshot

    def __post_init__(self) -> None:
        if self.environment is not None:
            self.environment = dict(self.environment)


class CliAdapterBase(ProviderAdapter):
    """Shared execute()/cancel() loop. Subclasses supply command, parser and error table."""

    name: str
    cli_name: str  # executable name to resolve
    extra_executable_candidates: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        spawner: Spawner | None = None,
        env_allowlist: Iterable[str] | None = None,
        environ: dict[str, str] | None = None,
        resolver: Callable[[str], ResolvedExecutable] | None = None,
        data_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        self._spawner: Spawner = spawner or spawn_process
        # None = the effective data dir of this process (ORCHESTRATOR_DATA_DIR or the default), resolved lazily.
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._allowlist = list(env_allowlist) if env_allowlist is not None else load_env_allowlist()
        self._environ = environ  # None = live os.environ
        self._resolver = resolver
        self._procs: dict[str, ProcessHandle] = {}
        self._spawns: dict[str, asyncio.Task[ProcessHandle]] = {}
        self._cancelled: set[str] = set()

    # ---- hooks for subclasses
    def build_command(self, request: ExecutionRequest) -> CommandSpec:
        raise NotImplementedError

    def parse_line(self, line: str, state: ParseState) -> list[AgentEvent]:
        raise NotImplementedError

    def error_table(self) -> list[tuple[ErrorCategory, tuple[str, ...]]]:
        raise NotImplementedError

    def finalize_state(self, state: ParseState, request: ExecutionRequest) -> None:
        """Optional post-processing (e.g. parse JSON structured output)."""

    async def before_spawn(self, request: ExecutionRequest, spec: CommandSpec) -> None:
        """Optional invocation-local preflight, immediately before process creation."""

    # ---- request safety (shared by every adapter)
    @property
    def data_dir(self) -> Path:
        if self._data_dir is None:
            from orchestrator.config import get_paths

            self._data_dir = get_paths().data_dir
        return self._data_dir

    def protected_dirs(self) -> list[Path]:
        from orchestrator.config import install_dirs

        return [self.data_dir, *install_dirs()]

    def check_request(self, request: ExecutionRequest) -> None:
        """Raise InvalidRequestError for an unsafe request. Called before any command is built or spawned."""
        validate_session_id(request.resume_session_id)
        check_run_paths(request.cwd, request.extra_dirs, self.protected_dirs())

    # ---- shared
    def resolve(self) -> ResolvedExecutable:
        if self._resolver:
            return self._resolver(self.cli_name)
        return resolve_executable(self.cli_name, extra_candidates=self.extra_executable_candidates)

    def child_env(self) -> dict[str, str]:
        base = self._environ if self._environ is not None else dict(os.environ)
        return build_child_env(self._allowlist, base_env=base)

    def classify_error(self, exit_code: int | None, stdout_tail: str, stderr_tail: str) -> ErrorCategory:
        text = f"{stderr_tail}\n{stdout_tail}"
        return classify_by_table(self.error_table(), exit_code, text)

    def pid_for(self, run_id: str) -> int | None:
        proc = self._procs.get(run_id)
        return proc.pid if proc else None

    async def cancel(self, run_id: str) -> bool:
        proc = self._procs.get(run_id)
        if proc is None:
            pending = self._spawns.get(run_id)
            if pending is not None:
                self._cancelled.add(run_id)
                if not pending.cancelling():
                    pending.cancel()  # one permanent veto; never interrupt the resulting native abort again
                return True
            return False
        self._cancelled.add(run_id)
        return await proc.terminate_tree()

    @staticmethod
    def _result_event(result: ExecutionResult) -> AgentEvent:
        return AgentEvent(kind=AgentEventKind.RESULT, data=result.model_dump(mode="json"))

    async def execute(self, request: ExecutionRequest) -> AsyncIterator[AgentEvent]:  # type: ignore[override]
        environ = self._environ if self._environ is not None else os.environ
        check_recursion_guard(dict(environ))  # raises RecursionGuardError by design (orchestrator bug, not provider failure)

        started = time.monotonic()
        state = ParseState()
        spec: CommandSpec | None = None
        proc: ProcessHandle | None = None

        def fail(category: ErrorCategory, message: str, exit_code: int | None = None) -> list[AgentEvent]:
            res = ExecutionResult(
                success=False, final_text=state.text(), session_id=state.session_id, model=state.model,
                exit_code=exit_code, error_category=category, error_message=message[:2000], usage=state.usage,
                duration_seconds=round(time.monotonic() - started, 3),
            )
            return [
                AgentEvent(kind=AgentEventKind.ERROR, data={"category": category.value, "message": message[:2000]}),
                self._result_event(res),
            ]

        try:
            try:
                self.check_request(request)
                spec = self.build_command(request)
                exe = self.resolve()
            except UnsupportedOperation as exc:
                for ev in fail(ErrorCategory.INVALID_REQUEST, f"unsupported: {exc}"):
                    yield ev
                return
            except ExecutableResolutionError as exc:
                for ev in fail(ErrorCategory.CRASHED, str(exc)):
                    yield ev
                return
            argv = [*exe.argv_prefix, *spec.args]
            try:
                await self.before_spawn(request, spec)
                if request.run_id in self._cancelled:
                    for ev in fail(ErrorCategory.CANCELLED, "run cancelled before process creation"):
                        yield ev
                    return
                pending_spawn = asyncio.create_task(self._spawner(
                    argv, cwd=request.cwd,
                    env=dict(spec.environment) if spec.environment is not None else self.child_env(),
                    stdin_text=spec.stdin_text,
                    timeout=request.timeout_seconds,
                ))
                self._spawns[request.run_id] = pending_spawn
                try:
                    proc = await pending_spawn
                except asyncio.CancelledError:
                    # The Task can complete before its awaiter is interrupted.
                    # Adopt its returned handle even when cancellation wins delivery.
                    if not pending_spawn.done():
                        raise worker_job.ContainmentError("cancelled process creation remains unresolved") from None
                    if not pending_spawn.cancelled():
                        proc = pending_spawn.result()
                        self._procs[request.run_id] = proc
                    if request.run_id not in self._cancelled:
                        raise
                    for ev in fail(ErrorCategory.CANCELLED, "run cancelled during process creation"):
                        yield ev
                    return
                finally:
                    self._spawns.pop(request.run_id, None)
            except worker_job.ContainmentError:
                raise
            except (OSError, ValueError) as exc:
                for ev in fail(ErrorCategory.CRASHED, f"failed to start {self.cli_name}: {exc}"):
                    yield ev
                return
            self._procs[request.run_id] = proc

            if request.run_id in self._cancelled:
                await proc.terminate_tree()  # cancellation may have arrived while spawn was pending

            async for kind, line in proc.stream():
                if kind != "stdout":
                    continue
                try:
                    events = self.parse_line(line, state)
                except Exception as exc:  # noqa: BLE001 - a parser bug must not lose the run
                    log.warning("%s parser failed on a line: %s", self.name, exc)
                    events = [raw_event(line, f"parser error: {exc}")]
                for ev in events:
                    yield ev
            exit_code = await proc.wait()
            self.finalize_state(state, request)

            cancelled = request.run_id in self._cancelled
            ok = state.success is True and exit_code == 0 and not proc.timed_out and not cancelled
            if ok:
                yield self._result_event(ExecutionResult(
                    success=True, final_text=state.text(), structured_output=state.structured_output,
                    session_id=state.session_id, model=state.model, exit_code=exit_code, usage=state.usage,
                    duration_seconds=round(time.monotonic() - started, 3),
                ))
                return
            if cancelled:
                category, message = ErrorCategory.CANCELLED, "run cancelled"
            elif proc.timed_out:
                category, message = ErrorCategory.TIMEOUT, f"run exceeded timeout of {request.timeout_seconds}s"
            else:
                err_text = "\n".join(state.error_messages)
                stdout_for_cls = err_text if err_text else proc.stdout_tail
                category = self.classify_error(exit_code, stdout_for_cls, proc.stderr_tail)
                if state.success is False and category in (ErrorCategory.UNKNOWN,) and state.permission_denials:
                    category = ErrorCategory.PERMISSION_DENIED
                if state.success is None and exit_code == 0 and category is ErrorCategory.UNKNOWN:
                    category = ErrorCategory.CRASHED
                    err_text = err_text or "provider exited without reporting a result"
                message = err_text or proc.stderr_tail.strip()[-1500:] or f"{self.cli_name} exited with code {exit_code}"
            for ev in fail(category, message, exit_code):
                yield ev
        finally:
            self._cancelled.discard(request.run_id)
            handle = self._procs.get(request.run_id, proc)
            if handle is not None:
                try:  # parent exit never proves descendant termination
                    await handle.terminate_tree()
                except worker_job.ContainmentError:
                    raise
                except Exception as exc:
                    raise worker_job.ContainmentError(f"provider lifetime cleanup failed: {exc}") from exc
                self._procs.pop(request.run_id, None)
            if spec:
                for p in spec.cleanup:
                    try:
                        p.unlink(missing_ok=True)
                    except OSError:
                        pass
