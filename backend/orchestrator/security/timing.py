"""Bounded, request-local native verification timings; never capture task inputs."""

from __future__ import annotations

import functools
import json
import logging
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar

STAGES = frozenset({"native_queue", "native_operation", "prepare", "start", "finish",
                    "runtime_lock_wait", "runtime_stage", "runtime_acquire", "runtime_pin",
                    "runtime_validate", "runtime_hash", "runtime_acl", "runtime_reuse", "runtime_recheck", "profile", "spawn", "resume",
                    "child_observation", "cleanup"})


class Recorder:
    def __init__(self):
        self._lock = threading.Lock()
        self._values: dict[str, dict[str, float | int]] = {}

    def add(self, stage: str, seconds: float) -> None:
        if stage not in STAGES:
            raise ValueError("unknown verification timing stage")
        with self._lock:
            value = self._values.setdefault(stage, {"seconds": 0.0, "calls": 0})
            value["seconds"] += max(0.0, seconds)
            value["calls"] += 1

    def snapshot(self):
        with self._lock:
            return {stage: {"seconds": round(value["seconds"], 6), "calls": value["calls"]}
                    for stage, value in sorted(self._values.items())}


current: ContextVar[Recorder | None] = ContextVar("verification_timing", default=None)


def clock() -> float | None:
    try:
        return time.monotonic()
    except Exception:
        return None


def elapsed(stage: str, started: float | None) -> None:
    try:
        recorder = current.get()
        if recorder is not None and started is not None:
            recorder.add(stage, time.monotonic() - started)
    except Exception:
        pass  # Never lose a created resource or mask a native/security error for diagnostics.


@contextmanager
def measure(stage: str):
    started = clock()
    try:
        yield
    finally:
        elapsed(stage, started)


def timed(stage: str):
    if stage not in STAGES:
        raise ValueError("unknown verification timing stage")

    def decorate(operation):
        @functools.wraps(operation)
        def run(*args, **kwargs):
            with measure(stage):
                return operation(*args, **kwargs)
        return run
    return decorate


@contextmanager
def lock_wait(lock):
    started = clock()
    lock.acquire()
    try:
        elapsed("runtime_lock_wait", started)
        yield
    finally:
        lock.release()


def capture_log(logger: logging.Logger):
    """One summary after joined cleanup, including failure/cancellation; no args or errors."""
    def decorate(operation):
        @functools.wraps(operation)
        async def run(*args, **kwargs):
            recorder = Recorder()
            token = current.set(recorder)
            try:
                return await operation(*args, **kwargs)
            finally:
                current.reset(token)
                try:
                    logger.info("verification_timing %s", json.dumps(recorder.snapshot(), sort_keys=True))
                except Exception:
                    pass  # Diagnostics cannot change cancellation, containment or verification outcomes.
        return run
    return decorate
