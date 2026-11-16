"""Synthetic handle failures must not be reported as settled runtime cleanup."""

import ctypes
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.security import python_runtime as runtime
from orchestrator.security import sandbox


@pytest.fixture
def leases(monkeypatch, tmp_path):
    calls = []
    failing = set()
    created = []

    class Info(ctypes.Structure):
        # Full BY_HANDLE_FILE_INFORMATION layout: pin identity reads volume, file id and link count.
        _fields_ = [("dwFileAttributes", ctypes.c_ulong), ("ftCreationTime", ctypes.c_ulonglong),
                    ("ftLastAccessTime", ctypes.c_ulonglong), ("ftLastWriteTime", ctypes.c_ulonglong),
                    ("dwVolumeSerialNumber", ctypes.c_ulong), ("nFileSizeHigh", ctypes.c_ulong),
                    ("nFileSizeLow", ctypes.c_ulong), ("nNumberOfLinks", ctypes.c_ulong),
                    ("nFileIndexHigh", ctypes.c_ulong), ("nFileIndexLow", ctypes.c_ulong)]

    next_handle = 101

    def create(*args):
        nonlocal next_handle
        handle = next_handle
        next_handle += 1
        return handle

    def info(handle, output):
        out = ctypes.cast(output, ctypes.POINTER(Info)).contents
        out.dwFileAttributes, out.nNumberOfLinks, out.nFileIndexLow = 0, 1, handle
        return True

    def close(handle):
        calls.append(handle)
        if "exception" in failing:
            raise OSError("synthetic close exception")
        return handle not in failing

    monkeypatch.setattr(sandbox, "_k32", SimpleNamespace(CloseHandle=close, CreateFileW=create,
                                                       GetFileInformationByHandle=info), raising=False)
    monkeypatch.setattr(sandbox, "_BY_HANDLE_FILE_INFORMATION", Info, raising=False)
    monkeypatch.setattr(sandbox, "INVALID_HANDLE_VALUE", -1, raising=False)
    tree = tmp_path / "synthetic-runtime"
    tree.mkdir()
    for name in ["one", "two", "three"]:
        (tree / name).write_bytes(b"synthetic")

    def make(prepared=True):
        lease = runtime.RuntimeLease(tmp_path / "owned" / "runtimes")
        lease._held.callback(lambda: calls.append("guards"))
        if prepared:
            lease.executable = Path(lease.parent) / "python-v2" / "python.exe"
            lease._hold_tree(tree)  # production pin registration with fake native handles
        created.append(lease)
        return lease

    yield make, calls, failing
    # Explicit fake-only teardown. Production has no retention override/recovery shortcut.
    for lease in created:
        if hasattr(runtime, "_retained_runtime_leases"):
            runtime._retained_runtime_leases.discard(lease)
        if hasattr(lease, "_pins"):
            lease._pins.clear()
        lease._pending_pin = None
        lease._held.close()


def test_all_file_pins_close_before_directory_guards(leases):
    make, calls, _ = leases
    lease = make()
    lease.close()
    assert calls == [103, 102, 101, "guards"]
    assert lease.executable is None and getattr(lease, "_pins", []) == []
    lease.close()
    assert calls == [103, 102, 101, "guards"]


def test_failed_file_close_retains_ownership_guards_and_permanent_veto(leases, monkeypatch):
    make, calls, failing = leases
    lease = make()
    executable = lease.executable
    failing.add(102)
    with pytest.raises(runtime.RuntimeUnavailable, match="retained"):
        lease.close()
    assert calls == [103, 102]
    assert lease._pins == [101, 102] and lease.executable == executable
    assert lease in runtime._retained_runtime_leases
    assert len(lease._held._exit_callbacks) == 1
    # Never retry a failed numeric handle: it may have become invalid/reused.
    with pytest.raises(runtime.RuntimeUnavailable):
        lease.close()
    monkeypatch.setattr(runtime.paths, "directory_guard", lambda *a, **k: pytest.fail("unsafe reacquire"))
    with pytest.raises(runtime.RuntimeUnavailable):
        lease.acquire()
    other = runtime.RuntimeLease(lease.parent)
    with pytest.raises(runtime.RuntimeUnavailable):
        other.acquire()
    if os.name == "nt":
        case_alias = runtime.RuntimeLease(lease.parent, runtime_name=lease.runtime_name.upper())
        with pytest.raises(runtime.RuntimeUnavailable):
            case_alias.acquire()
    assert calls == [103, 102]


def test_native_close_exception_is_fail_closed(leases):
    make, calls, failing = leases
    lease = make()
    failing.add("exception")
    with pytest.raises(runtime.RuntimeUnavailable, match="retained"):
        lease.close()
    assert lease._pins == [101, 102, 103] and "guards" not in calls
    assert lease in runtime._retained_runtime_leases


def test_refused_reacquire_does_not_release_existing_healthy_lease(leases):
    make, calls, failing = leases
    failed, healthy = make(), make()
    failing.add(102)
    with pytest.raises(runtime.RuntimeUnavailable):
        failed.close()
    before = list(calls)
    with pytest.raises(runtime.RuntimeUnavailable):
        healthy.acquire()
    assert calls == before
    assert healthy.executable is not None and healthy._pins == [104, 105, 106]
    healthy.close()  # its owner may still explicitly drain and close it
    assert healthy.executable is None


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_acquisition_interrupt_survives_retained_cleanup_failure(leases, monkeypatch, interrupt):
    make, calls, failing = leases
    lease = make(prepared=False)
    destination = lease.parent / lease.runtime_name
    destination.mkdir(parents=True)
    for name in ["one", "two", "runtime-manifest.json"]:  # a complete (manifested) copy, never restaged
        (destination / name).write_bytes(b"synthetic")

    @contextmanager
    def guard(path):
        yield str(path)

    stop = interrupt("synthetic interruption")

    def interrupted(*args, **kwargs):
        raise stop

    monkeypatch.setattr(runtime.paths, "directory_guard", guard)
    monkeypatch.setattr(runtime.acl, "check_private", lambda path: [])
    monkeypatch.setattr(runtime, "validate_runtime", interrupted)
    failing.add(102)
    with pytest.raises(interrupt) as caught:
        lease.acquire()
    assert caught.value is stop
    assert lease in runtime._retained_runtime_leases
    assert lease._pins == [101, 102] and calls == [103, 102]


@pytest.mark.parametrize("close_fails", [False, True])
def test_registration_memory_error_keeps_handle_owned(leases, tmp_path, close_fails):
    make, calls, failing = leases
    lease = make(prepared=False)
    tree = tmp_path / "registration"
    tree.mkdir()
    (tree / "payload").write_bytes(b"synthetic")

    class NoMemory(list):
        def append(self, value):
            raise MemoryError("synthetic allocation failure")

    lease._pins = NoMemory()
    with pytest.raises(MemoryError):
        lease._hold_tree(tree)
    assert lease._pending_pin == 101
    if close_fails:
        failing.add(101)
        with pytest.raises(runtime.RuntimeUnavailable):
            lease.close()
        assert lease._pending_pin == 101 and calls == [101]
        assert lease in runtime._retained_runtime_leases
    else:
        lease.close()
        assert lease._pending_pin is None and calls == [101, "guards"]


def test_interruption_after_registration_never_double_closes(leases, tmp_path):
    make, calls, _ = leases
    lease = make(prepared=False)
    tree = tmp_path / "registration"
    tree.mkdir()
    (tree / "payload").write_bytes(b"synthetic")

    class InterruptedAppend(list):
        def append(self, value):
            super().append(value)
            raise KeyboardInterrupt("synthetic interruption after registration")

    lease._pins = InterruptedAppend()
    with pytest.raises(KeyboardInterrupt):
        lease._hold_tree(tree)
    lease.close()
    assert calls == [101, "guards"] and lease._pending_pin is None
