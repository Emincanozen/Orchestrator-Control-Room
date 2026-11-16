"""Worker containment record: which processes were started for agents (SECURITY_REVIEW #1).

The API must refuse requests that come from agent worker processes (and anything they start), because a
same-user process can always read the API token. This module is the "who is a worker" half of that guard;
`security/peer.py` resolves the process behind a TCP connection and `api/security.py` decides.

Windows: one unnamed Job Object per server process, created lazily on the first spawn. Every provider run,
helper command (`--version`, `auth status`, model listing) and verification command is created
SUSPENDED, assigned to the job and only then resumed, so it cannot start a child outside the job.
Job membership is inherited by every descendant. The job has no BREAKAWAY_OK / SILENT_BREAKAWAY_OK
limit, so `CREATE_BREAKAWAY_FROM_JOB` fails inside it. It has KILL_ON_JOB_CLOSE: the server holds the only
handle (not inheritable), so when the server process ends - cleanly or by a crash - every worker tree ends
with it. Startup recovery already assumes "no process survives a restart"; without the job a crashed server
could leave orphans that it kills only on the next start.

POSIX (best effort; the product is Windows-first): no job objects. Spawned PIDs are tracked with their
create time and a peer is a worker when it is a tracked PID or a descendant of one. Descendants re-parented
to init after their worker parent exited are not recognised.

Fail closed on Windows (re-review N5): the job is created eagerly at app startup (`ensure_job()`; the app
refuses to start when it cannot be created), and when a spawned process cannot be assigned to the job it is
NOT resumed: `confine_spawned` raises OSError and the caller kills the still-suspended process and fails the
spawn. The PID-tracking record is the POSIX mechanism only.

`run_confined()` is the synchronous counterpart of the async spawn paths (suspended -> assign -> resume ->
communicate). The orchestrator's own git subprocesses use it, so anything a repository manages to make git
execute is a job member too and is refused by the API's worker-origin guard.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from typing import Any, Optional, Sequence

import psutil

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
CREATE_SUSPENDED = 0x00000004
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

_lock = threading.Lock()
_job_handle: Optional[int] = None
_job_failed = False
_tracked: dict[int, float] = {}  # pid -> create_time (POSIX only)


class ContainmentError(OSError):
    """A worker lifetime boundary could not be established or proved empty.

    Callers must keep the affected worktree quarantined on this error. In
    particular, a parent's exit code is not evidence that its descendants died.
    """


_retained_jobs: set["SpawnJob"] = set()
DRAIN_TIMEOUT = 10.0

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes as wt

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    PROCESS_TERMINATE = 0x0001
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JobObjectExtendedLimitInformation = 9

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wt.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wt.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wt.DWORD),
            ("SchedulingClass", wt.DWORD),
        ]

    class _EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC_LIMIT),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
    _k32.CreateJobObjectW.restype = wt.HANDLE
    _k32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
    _k32.SetInformationJobObject.restype = wt.BOOL
    _k32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    _k32.AssignProcessToJobObject.restype = wt.BOOL
    _k32.IsProcessInJob.argtypes = [wt.HANDLE, wt.HANDLE, ctypes.POINTER(wt.BOOL)]
    _k32.IsProcessInJob.restype = wt.BOOL
    _k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    _k32.OpenProcess.restype = wt.HANDLE
    _k32.CloseHandle.argtypes = [wt.HANDLE]
    _k32.CloseHandle.restype = wt.BOOL
    _k32.TerminateJobObject.argtypes = [wt.HANDLE, wt.UINT]
    _k32.TerminateJobObject.restype = wt.BOOL
    _k32.QueryInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                               wt.DWORD, ctypes.c_void_p]
    _k32.QueryInformationJobObject.restype = wt.BOOL

    class _ACCOUNTING(ctypes.Structure):
        _fields_ = [("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
                    ("ThisPeriodTotalUserTime", ctypes.c_longlong), ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                    ("TotalPageFaultCount", wt.DWORD), ("TotalProcesses", wt.DWORD),
                    ("ActiveProcesses", wt.DWORD), ("TotalTerminatedProcesses", wt.DWORD)]

    def _winerr(what: str) -> OSError:
        code = ctypes.get_last_error()
        return OSError(code, f"{what} failed: {ctypes.FormatError(code).strip()}")

    def _ensure_job() -> int:
        """The process-wide worker job (created on first use). Raises OSError when it cannot be created."""
        global _job_handle, _job_failed
        with _lock:
            if _job_handle is not None:
                return _job_handle
            h = _k32.CreateJobObjectW(None, None)  # unnamed, default (non-inheritable) security
            if not h:
                _job_failed = True
                raise _winerr("CreateJobObjectW")
            info = _EXTENDED_LIMIT()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE  # and NO breakaway flags
            if not _k32.SetInformationJobObject(h, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)):
                err = _winerr("SetInformationJobObject")
                _k32.CloseHandle(h)
                _job_failed = True
                raise err
            _job_handle = h
            return h

    def _open(pid: int, access: int) -> Optional[int]:
        h = _k32.OpenProcess(access, False, pid)
        return h or None

    def _assign_windows(pid: int) -> None:
        job = _ensure_job()
        h = _open(pid, PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION)
        if h is None:
            raise _winerr("OpenProcess")
        try:
            if not _k32.AssignProcessToJobObject(job, h):
                raise _winerr("AssignProcessToJobObject")
        finally:
            _k32.CloseHandle(h)

    def _in_job_windows(pid: int) -> Optional[bool]:
        """True/False for a process we can open; None when it cannot be opened (gone, other user, protected)."""
        job = _job_handle
        if job is None:
            return False
        h = _open(pid, PROCESS_QUERY_LIMITED_INFORMATION)
        if h is None:
            return None
        try:
            res = wt.BOOL(False)
            if not _k32.IsProcessInJob(h, job, ctypes.byref(res)):
                return None
            return bool(res.value)
        finally:
            _k32.CloseHandle(h)


def _track(pid: int) -> None:
    try:
        created = psutil.Process(pid).create_time()
    except psutil.Error:
        return
    with _lock:
        _tracked[pid] = created


def spawn_creationflags() -> int:
    """Extra creation flags for a worker spawn: start suspended on Windows (resumed by `confine_spawned`)."""
    return CREATE_SUSPENDED if IS_WINDOWS else 0


class SpawnJob:
    """Unnamed, non-inheritable per-spawn Windows job, without provider UI limits.

    No breakaway flags are enabled. Unproved jobs keep a strong process-wide
    reference and their handle: no destructor silently closes an uncertain job.
    The server job remains the outer crash boundary.
    """

    def __init__(self) -> None:
        if not IS_WINDOWS:
            raise ContainmentError("per-spawn Windows jobs are unavailable on this platform")
        self._lock = threading.RLock()
        self._handle = _k32.CreateJobObjectW(None, None)
        if not self._handle:
            raise ContainmentError(f"cannot create per-spawn job: {_winerr('CreateJobObjectW')}")
        self.drained = False
        self._spawn_pending = False
        with _lock:
            _retained_jobs.add(self)
        limits = _EXTENDED_LIMIT()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _k32.SetInformationJobObject(self._handle, JobObjectExtendedLimitInformation,
                                            ctypes.byref(limits), ctypes.sizeof(limits)):
            error = _winerr("SetInformationJobObject(per-spawn)")
            self.close()  # even the empty initialization case needs drain proof
            raise ContainmentError(f"cannot establish per-spawn job limits: {error}")

    def assign(self, pid: int) -> None:
        with self._lock:
            if not self._handle or self.drained:
                raise ContainmentError("cannot assign a process to a completed lifetime job")
            handle = _open(pid, PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION)
            if handle is None:
                raise ContainmentError(f"cannot open suspended worker {pid}: {_winerr('OpenProcess')}")
            try:
                if not _k32.AssignProcessToJobObject(self._handle, handle):
                    raise ContainmentError(f"cannot assign worker {pid} to its lifetime job: "
                                           f"{_winerr('AssignProcessToJobObject')}")
            finally:
                _k32.CloseHandle(handle)

    def _active_processes(self) -> int:
        accounting = _ACCOUNTING()
        if not _k32.QueryInformationJobObject(self._handle, 1, ctypes.byref(accounting),
                                              ctypes.sizeof(accounting), None):
            raise ContainmentError(f"cannot prove per-spawn job drain: {_winerr('QueryInformationJobObject')}")
        return accounting.ActiveProcesses

    def begin_spawn(self) -> None:
        with self._lock:
            if not self._handle or self.drained:
                raise ContainmentError("cannot spawn into a completed lifetime job")
            self._spawn_pending = True

    def end_spawn(self) -> None:
        with self._lock:
            self._spawn_pending = False

    def close(self) -> bool:
        """Terminate, prove ActiveProcesses==0, then close; safe to retry on failure."""
        with self._lock:
            if not self._handle:
                return False
            # A failed query must not prevent best-effort termination.
            if not _k32.TerminateJobObject(self._handle, 1):
                raise ContainmentError(f"cannot terminate per-spawn job: {_winerr('TerminateJobObject')}")
            if self._spawn_pending:
                raise ContainmentError("spawn creation is unresolved; lifetime job retained")
            deadline = time.monotonic() + DRAIN_TIMEOUT
            while self._active_processes():
                if time.monotonic() >= deadline:
                    raise ContainmentError("per-spawn job still has active processes after termination")
                time.sleep(0.01)
            self.drained = True
            if not _k32.CloseHandle(self._handle):
                raise ContainmentError(f"cannot close drained per-spawn job: {_winerr('CloseHandle')}")
            self._handle = None
            with _lock:
                _retained_jobs.discard(self)
            return True


def resume_spawned(pid: int) -> None:
    """Resume only after all required job assignments have completed."""
    if IS_WINDOWS:
        try:
            psutil.Process(pid).resume()
        except psutil.Error as exc:
            raise OSError(f"could not resume worker process {pid}: {exc}") from exc


def confine_spawned(pid: int, *, suspended: bool) -> bool:
    """Record a freshly spawned worker process (assign it to the job on Windows), then resume it if it was
    created suspended. Returns True when the process is in the job (Windows) or tracked (POSIX).

    Raises OSError when the process could not be assigned to the job (Windows; it is left suspended) or could
    not be resumed. The caller must kill it and fail the spawn."""
    if IS_WINDOWS:
        try:
            _assign_windows(pid)
        except OSError as exc:
            # Fail closed: the process stays suspended (it never ran a single instruction); the caller kills it.
            log.error("SECURITY: could not assign spawned pid %s to the worker job (%s); the spawn is refused", pid, exc)
            raise OSError(f"could not confine process {pid} in the worker job: {exc}") from exc
    else:
        _track(pid)
    if suspended and IS_WINDOWS:
        resume_spawned(pid)
    return True


def ensure_job() -> None:
    """Create the worker job now (Windows; no-op elsewhere). Raises RuntimeError when it cannot be created: the
    server must not start, because every later spawn would have to be refused anyway."""
    if not IS_WINDOWS:
        return
    try:
        _ensure_job()
    except OSError as exc:
        raise RuntimeError(
            f"SECURITY: cannot create the Windows Job Object that confines agent processes ({exc}); refusing to "
            "start. Without it the API cannot tell agent processes from the user."
        ) from exc


def _kill_tree(proc: "subprocess.Popen[Any]") -> None:
    try:
        root = psutil.Process(proc.pid)
        procs = [*root.children(recursive=True), root]
    except psutil.Error:
        procs = []
    for p in procs:
        try:
            p.kill()
        except psutil.Error:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def run_confined(
    argv: Sequence[str],
    *,
    cwd: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    timeout: Optional[float] = None,
    input: Any = None,  # noqa: A002 - mirrors subprocess.run
    text: bool = False,
    encoding: Optional[str] = None,
    errors: Optional[str] = None,
    creationflags: int = 0,
) -> "subprocess.CompletedProcess[Any]":
    """`subprocess.run(argv, capture_output=True, ...)` with the child confined like a worker before it runs:
    created suspended, assigned to the worker job, then resumed (Windows); tracked on POSIX.

    stdin is a pipe when `input` is given, otherwise DEVNULL. Raises subprocess.TimeoutExpired (after killing
    the process tree) and OSError (spawn or confinement failure; the process is killed first)."""
    kw: dict[str, Any] = {}
    lifetime = SpawnJob() if IS_WINDOWS else None
    if IS_WINDOWS:
        kw["creationflags"] = creationflags | CREATE_SUSPENDED
    else:
        kw["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            list(argv), cwd=cwd, env=env, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=text or encoding is not None or errors is not None,
            encoding=encoding, errors=errors, **kw)
    except BaseException:
        if lifetime is not None:
            lifetime.close()
        raise
    try:
        try:
            confine_spawned(proc.pid, suspended=False)
            if lifetime is not None:
                lifetime.assign(proc.pid)
                resume_spawned(proc.pid)
        except OSError:
            _kill_tree(proc)
            if lifetime is not None:
                lifetime.close()
            try:
                proc.communicate(timeout=DRAIN_TIMEOUT + 2)
            except subprocess.TimeoutExpired as exc:
                raise ContainmentError("failed synchronous spawn could not be reaped") from exc
            raise
        deadline = time.monotonic() + timeout if timeout is not None else None
        pending_input = input
        while True:
            try:
                # Do not let an inherited stdout/stderr handle hide parent exit.
                remaining = max(0.001, deadline - time.monotonic()) if deadline is not None else 0.1
                out, err = proc.communicate(pending_input, timeout=min(0.1, remaining))
                break
            except subprocess.TimeoutExpired as exc:
                pending_input = None
                if proc.poll() is not None:
                    if lifetime is not None:
                        lifetime.close()
                    else:
                        _kill_tree(proc)
                    try:
                        out, err = proc.communicate(timeout=DRAIN_TIMEOUT + 2)
                    except subprocess.TimeoutExpired as drain:
                        raise ContainmentError("synchronous worker pipes did not finish after parent exit") from drain
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    if lifetime is not None:
                        lifetime.close()
                    else:
                        _kill_tree(proc)
                    try:
                        proc.communicate(timeout=DRAIN_TIMEOUT + 2)
                    except subprocess.TimeoutExpired as drain:
                        raise ContainmentError("timed-out synchronous worker could not be reaped") from drain
                    raise subprocess.TimeoutExpired(list(argv), timeout, output=exc.output, stderr=exc.stderr) from exc
    except BaseException:
        if proc.poll() is None:
            _kill_tree(proc)
        raise
    finally:
        if lifetime is not None:
            lifetime.close()
        else:
            _kill_tree(proc)
    return subprocess.CompletedProcess(list(argv), proc.returncode, out, err)


def active() -> bool:
    """True once any worker was spawned in this process (otherwise no peer can be a worker)."""
    return _job_handle is not None or bool(_tracked) or _job_failed


def is_in_job(pid: int) -> Optional[bool]:
    """Windows: job membership of `pid` (None = cannot tell). POSIX: always False."""
    if IS_WINDOWS:
        return _in_job_windows(pid)
    return False


def _tracked_alive() -> dict[int, float]:
    with _lock:
        items = list(_tracked.items())
    alive: dict[int, float] = {}
    dead: list[int] = []
    for pid, created in items:
        try:
            if abs(psutil.Process(pid).create_time() - created) < 1.0:
                alive[pid] = created
                continue
        except psutil.Error:
            pass
        dead.append(pid)
    if dead:
        with _lock:
            for pid in dead:
                _tracked.pop(pid, None)
    return alive


def is_tracked_or_descendant(pid: int) -> bool:
    """The fallback record: `pid` is a tracked worker PID or one of its live descendants."""
    tracked = _tracked_alive()
    if not tracked:
        return False
    if pid in tracked:
        return True
    try:
        proc = psutil.Process(pid)
        child_created = proc.create_time()
        for parent in proc.parents():
            created = tracked.get(parent.pid)
            if created is not None and created <= child_created + 1.0:
                return True
    except psutil.Error:
        return False
    return False


def is_worker(pid: int) -> Optional[bool]:
    """True when `pid` is a worker process (job member, tracked, or descendant of tracked); None = cannot tell."""
    if pid == os.getpid():
        return False
    member = is_in_job(pid)
    if member:
        return True
    if is_tracked_or_descendant(pid):
        return True
    return member  # False, or None when the process could not be inspected


def _reset_for_tests() -> None:
    """Forget the tracked set (the job itself is kept: closing it would kill its members)."""
    with _lock:
        _tracked.clear()


__all__ = [
    "CREATE_BREAKAWAY_FROM_JOB", "CREATE_SUSPENDED", "ContainmentError", "SpawnJob", "resume_spawned",
    "active", "confine_spawned", "ensure_job", "is_in_job",
    "is_worker", "is_tracked_or_descendant", "run_confined", "spawn_creationflags",
]
