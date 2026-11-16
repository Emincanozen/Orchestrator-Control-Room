"""Background server management for `orchestrator dashboard` / `stop`.

Pid file `<data dir>/server.pid`: line 1 = pid, line 2 = port, line 3 = process create time. `stop` only ever
acts on the pid in that file, after checking that the process command line is really `orchestrator serve`
(and that the create time still matches, so a recycled pid is never touched). Never kills by name.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import psutil

from orchestrator.config import get_paths

# Windows creation flags (0 elsewhere): no console window, own process group, leave the caller's job object.
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def pid_file() -> Path:
    return get_paths().data_dir / "server.pid"


def log_file() -> Path:
    return get_paths().logs_dir / "server.log"


@dataclass
class PidRecord:
    pid: int
    port: Optional[int]
    created: Optional[float]


def read_pid_file() -> Optional[PidRecord]:
    try:
        lines = pid_file().read_text(encoding="ascii").splitlines()
        pid = int(lines[0].strip())
    except (OSError, ValueError, IndexError):
        return None

    def num(i: int, cast):
        try:
            return cast(lines[i].strip())
        except (IndexError, ValueError):
            return None

    return PidRecord(pid=pid, port=num(1, int), created=num(2, float))


def write_pid_file(pid: int, port: int) -> None:
    created = ""
    try:
        created = repr(psutil.Process(pid).create_time())
    except psutil.Error:
        pass
    path = pid_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n{port}\n{created}\n", encoding="ascii")


def remove_pid_file() -> None:
    try:
        pid_file().unlink()
    except OSError:
        pass


def is_our_server(rec: PidRecord) -> tuple[bool, str]:
    """(True, '') when rec.pid is a live `orchestrator serve` process we started; else (False, reason)."""
    try:
        proc = psutil.Process(rec.pid)
        cmd = proc.cmdline()
        created = proc.create_time()
    except psutil.NoSuchProcess:
        return False, "process is not running"
    except psutil.Error as exc:
        return False, f"cannot inspect process {rec.pid}: {exc.__class__.__name__}"
    if rec.created is not None and abs(created - rec.created) > 2.0:
        return False, f"pid {rec.pid} now belongs to a different process (stale pid file)"
    joined = " ".join(cmd).lower()
    entry = os.path.basename(cmd[0]).lower() if cmd else ""
    is_orch = "orchestrator.cli.main" in joined or entry.startswith("orchestrator")
    if not (is_orch and "serve" in [a.lower() for a in cmd[1:]]):
        return False, f"pid {rec.pid} is not an `orchestrator serve` process (command line does not match)"
    return True, ""


def stop_pid(pid: int, timeout: float = 15.0) -> bool:
    """Terminate that exact pid and its descendants (venv launcher stub -> real python, provider children).
    Returns True when everything is gone."""
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True
    procs = parent.children(recursive=True) + [parent]
    for p in procs:
        try:
            p.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(procs, timeout=timeout)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(alive, timeout=5)
    return not alive


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def spawn_server(port: int) -> subprocess.Popen:
    """Start `python -m orchestrator.cli.main serve --port N --no-browser` detached, logging to server.log."""
    log = log_file()
    log.parent.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, "-m", "orchestrator.cli.main", "serve", "--port", str(port), "--no-browser", "--no-access-log"]
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    with open(log, "ab") as fh:
        kw: dict = {"stdin": subprocess.DEVNULL, "stdout": fh, "stderr": subprocess.STDOUT, "env": env, "close_fds": True}
        if sys.platform == "win32":
            base = _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
            try:
                return subprocess.Popen(argv, creationflags=base | _CREATE_BREAKAWAY_FROM_JOB, **kw)
            except OSError:  # the parent's job object forbids breakaway
                return subprocess.Popen(argv, creationflags=base, **kw)
        return subprocess.Popen(argv, start_new_session=True, **kw)


def wait_until(predicate, timeout: float, interval: float = 0.4) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def log_tail(lines: int = 15) -> str:
    try:
        text = log_file().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])
