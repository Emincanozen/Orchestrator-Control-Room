"""Single-instance lock on the data directory (SECURITY_REVIEW #10).

Two servers on one data dir would each run crash recovery (killing the other's live runs) and each start a
scheduler dispatching from the same database. The lock is an exclusive OS-level byte-range lock on
`<data_dir>/server.lock`, held on an open handle for the server's whole lifetime:

* Windows: `msvcrt.locking(LK_NBLCK)` on byte 0 of the open handle;
* POSIX:   `fcntl.flock(LOCK_EX | LOCK_NB)`.

OS locks vanish with the process, so a stale `server.lock` left by a crashed process never blocks startup. A
second acquisition (another process, or another handle in this process) fails fast with `InstanceLockError`.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Optional

LOCK_NAME = "server.lock"
_INFO_OFFSET = 64  # holder info lives beyond the locked byte so a loser can read it on Windows


class InstanceLockError(RuntimeError):
    """Another orchestrator instance already holds this data directory."""


def lock_path(data_dir: str | os.PathLike[str]) -> Path:
    return Path(data_dir) / LOCK_NAME


def _try_lock(fh) -> bool:
    if sys.platform == "win32":
        import msvcrt

        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fh) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


class InstanceLock:
    def __init__(self, data_dir: str | os.PathLike[str]):
        self.path = lock_path(data_dir)
        self._fh: Optional[object] = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def _holder(self) -> str:
        try:
            with open(self.path, "rb") as fh:
                fh.seek(_INFO_OFFSET)
                return fh.read(200).decode("utf-8", "replace").strip("\x00 \r\n")
        except OSError:
            return ""

    def acquire(self) -> "InstanceLock":
        if self._fh is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b", buffering=0)
        if not _try_lock(fh):
            fh.close()
            holder = self._holder()
            raise InstanceLockError(
                f"another AI Orchestrator instance is already using this data directory ({self.path.parent})"
                + (f" [{holder}]" if holder else "")
                + ". Stop it first, or point this one at another data directory (ORCHESTRATOR_DATA_DIR)."
            )
        try:
            fh.seek(_INFO_OFFSET)
            fh.truncate(_INFO_OFFSET)
            fh.write(f"pid={os.getpid()} started={time.strftime('%Y-%m-%dT%H:%M:%S')}".encode("ascii"))
        except OSError:  # diagnostics only
            pass
        self._fh = fh
        return self

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        _unlock(fh)
        try:
            fh.close()  # type: ignore[attr-defined]
        except OSError:
            pass

    def __enter__(self) -> "InstanceLock":
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()
