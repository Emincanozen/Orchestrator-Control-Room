"""Timing records must not leak inputs, cross requests, or alter trusted outcomes."""

import asyncio
import json
import logging
import threading
from types import SimpleNamespace

import pytest

from orchestrator.security import timing


async def test_timing_isolates_concurrent_requests_and_propagates_to_threads(caplog):
    logger = logging.getLogger("test.verification_timing")

    @timing.capture_log(logger)
    async def operation(stage, private):
        @timing.timed(stage)
        def native():
            return private
        return await asyncio.to_thread(native)

    with caplog.at_level(logging.INFO, logger=logger.name):
        assert await asyncio.gather(operation("prepare", "PRIVATE-FIRST"),
                                    operation("spawn", "PRIVATE-SECOND")) == ["PRIVATE-FIRST", "PRIVATE-SECOND"]
    records = [json.loads(r.message.removeprefix("verification_timing ")) for r in caplog.records]
    assert sorted(tuple(r) for r in records) == [("prepare",), ("spawn",)]
    assert all(next(iter(r.values()))["calls"] == 1 for r in records)
    assert "PRIVATE" not in caplog.text
    assert timing.current.get() is None


async def test_timing_keeps_original_exception_and_excludes_message(caplog):
    logger = logging.getLogger("test.verification_timing.failure")

    @timing.capture_log(logger)
    async def operation():
        with timing.measure("runtime_validate"):
            raise PermissionError("SECRET-PATH")

    with caplog.at_level(logging.INFO, logger=logger.name):
        with pytest.raises(PermissionError, match="SECRET-PATH"):
            await operation()
    assert "SECRET-PATH" not in caplog.text
    assert json.loads(caplog.records[-1].message.removeprefix("verification_timing "))["runtime_validate"]["calls"] == 1
    assert timing.current.get() is None


async def test_logging_failure_cannot_replace_cancellation():
    def broken(*args):
        raise RuntimeError("logger unavailable")

    @timing.capture_log(SimpleNamespace(info=broken))
    async def operation():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await operation()
    assert timing.current.get() is None


def test_recorder_rejects_dynamic_labels_and_bounds_snapshot():
    recorder = timing.Recorder()
    with pytest.raises(ValueError, match="^unknown verification timing stage$"):
        recorder.add("PRIVATE-PATH", 1)
    for _ in range(1000):
        recorder.add("runtime_hash", 0.001)
    snapshot = recorder.snapshot()
    assert snapshot == {"runtime_hash": {"seconds": 1.0, "calls": 1000}}
    snapshot["runtime_hash"]["calls"] = 0
    assert recorder.snapshot()["runtime_hash"]["calls"] == 1000


def test_measurement_failure_preserves_resource_return_and_exception(monkeypatch):
    def broken(*args):
        raise MemoryError("diagnostic storage unavailable")

    monkeypatch.setattr(timing.Recorder, "add", broken)
    token = timing.current.set(timing.Recorder())
    resource = object()
    original_error = PermissionError("native operation failed")

    @timing.timed("spawn")
    def create_resource():
        return resource

    @timing.timed("finish")
    def fail_cleanup():
        raise original_error

    try:
        assert create_resource() is resource
        with pytest.raises(PermissionError) as caught:
            fail_cleanup()
        assert caught.value is original_error
    finally:
        timing.current.reset(token)


def test_measurement_failure_cannot_leak_runtime_lock(monkeypatch):
    def broken(*args):
        raise MemoryError("diagnostic storage unavailable")

    monkeypatch.setattr(timing.Recorder, "add", broken)
    token = timing.current.set(timing.Recorder())
    lock = threading.Lock()
    try:
        with timing.lock_wait(lock):
            assert lock.locked()
        assert not lock.locked()
    finally:
        if lock.locked():
            lock.release()
        timing.current.reset(token)
