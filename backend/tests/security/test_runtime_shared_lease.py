"""Process-wide shared runtime lease: validated once, cheap re-check per acquire, poisoned on failure."""

import ctypes
import gc
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.security import python_runtime as runtime
from orchestrator.security import sandbox, timing


@pytest.fixture
def shared(monkeypatch, tmp_path):
    state = SimpleNamespace(validations=0, rechecks=0, closed=[], recheck_error=None, close_fails=set())

    class Info(ctypes.Structure):  # BY_HANDLE_FILE_INFORMATION layout
        _fields_ = [("dwFileAttributes", ctypes.c_ulong), ("ftCreationTime", ctypes.c_ulonglong),
                    ("ftLastAccessTime", ctypes.c_ulonglong), ("ftLastWriteTime", ctypes.c_ulonglong),
                    ("dwVolumeSerialNumber", ctypes.c_ulong), ("nFileSizeHigh", ctypes.c_ulong),
                    ("nFileSizeLow", ctypes.c_ulong), ("nNumberOfLinks", ctypes.c_ulong),
                    ("nFileIndexHigh", ctypes.c_ulong), ("nFileIndexLow", ctypes.c_ulong)]

    counter = iter(range(101, 10_000))

    def close(handle):
        state.closed.append(handle)
        return handle not in state.close_fails

    def info(handle, output):
        out = ctypes.cast(output, ctypes.POINTER(Info)).contents
        out.dwFileAttributes, out.nNumberOfLinks, out.nFileIndexLow = 0, 1, handle
        return True

    monkeypatch.setattr(sandbox, "_k32", SimpleNamespace(
        CloseHandle=close, CreateFileW=lambda *a: next(counter), GetFileInformationByHandle=info), raising=False)
    monkeypatch.setattr(sandbox, "_BY_HANDLE_FILE_INFORMATION", Info, raising=False)
    monkeypatch.setattr(sandbox, "INVALID_HANDLE_VALUE", -1, raising=False)

    @contextmanager
    def guard(path):
        yield str(path)

    def validate(destination, **kwargs):
        state.validations += 1  # stands in for the SHA-256 + ACL pass
        return destination / kwargs["required_executable"]

    def recheck(core):
        state.rechecks += 1
        if state.recheck_error:
            raise runtime.RuntimeUnavailable(state.recheck_error)

    monkeypatch.setattr(runtime, "_shared_runtimes", {})  # isolated from real runtimes shared by other tests
    monkeypatch.setattr(runtime.paths, "directory_guard", guard)
    monkeypatch.setattr(runtime.acl, "check_private", lambda path: [])
    monkeypatch.setattr(runtime, "validate_runtime", validate)
    monkeypatch.setattr(runtime, "_recheck_pins", recheck)

    parent = tmp_path / "data" / "runtimes"

    def stage(destination: Path):
        destination.mkdir()
        for name in ("python.exe", "lib"):
            (destination / name).write_bytes(b"x")

    def make():
        return runtime.RuntimeLease(parent, stage=stage, shared=True)

    yield make, state
    # Fake-only teardown; production releases through shutdown_shared_runtimes().
    for entry in runtime._shared_runtimes.values():
        runtime._retained_runtime_leases.discard(entry.core)
        entry.core._pins.clear()
        entry.core._held.close()
    runtime._shared_runtimes.clear()
    runtime._retained_runtime_leases.clear()


def test_second_acquire_does_not_revalidate_and_shares_pins(shared):
    make, state = shared
    first, second, third = make(), make(), make()
    exe = first.acquire()
    assert state.validations == 1 and state.rechecks == 0
    assert second.acquire() == exe and third.acquire() == exe
    assert state.validations == 1 and state.rechecks == 2
    assert len(runtime._shared_runtimes) == 1
    assert state.closed == []


def test_close_only_drops_reference_and_pins_survive(shared):
    make, state = shared
    first, second = make(), make()
    first.acquire()
    second.acquire()
    entry = next(iter(runtime._shared_runtimes.values()))
    assert entry.refs == 2
    first.close()
    first.close()  # idempotent
    assert entry.refs == 1 and first.executable is None and state.closed == []
    second.close()
    assert entry.refs == 0 and state.closed == []
    again = make()
    again.acquire()
    assert state.validations == 1 and state.rechecks == 2  # warm: pins kept for the server lifetime
    again.close()


def test_shutdown_releases_unreferenced_pins_and_next_acquire_revalidates(shared):
    make, state = shared
    lease = make()
    lease.acquire()
    assert runtime.shutdown_shared_runtimes() == 1  # still referenced
    lease.close()
    assert runtime.shutdown_shared_runtimes() == 0
    assert sorted(state.closed) == [101, 102] and not runtime._shared_runtimes
    fresh = make()
    fresh.acquire()
    assert state.validations == 2
    fresh.close()


def test_recheck_failure_is_refused_and_poisons_the_shared_runtime(shared):
    make, state = shared
    owner, victim, later = make(), make(), make()
    owner.acquire()
    state.recheck_error = "DACL changed"
    with pytest.raises(runtime.RuntimeUnavailable, match="DACL changed"):
        victim.acquire()
    assert victim.executable is None
    state.recheck_error = None  # even a healed check must not resurrect a poisoned entry
    with pytest.raises(runtime.RuntimeUnavailable, match="not reused"):
        later.acquire()
    assert state.validations == 1
    owner.close()  # last reference gone: pins released, next acquire starts from a full validation
    again = make()
    again.acquire()
    assert state.validations == 2 and sorted(state.closed) == [101, 102]
    again.close()


def test_first_validation_failure_leaves_nothing_shared(shared, monkeypatch):
    make, state = shared

    def broken(destination, **kwargs):
        raise runtime.RuntimeUnavailable("runtime content changed")

    monkeypatch.setattr(runtime, "validate_runtime", broken)
    with pytest.raises(runtime.RuntimeUnavailable, match="content changed"):
        make().acquire()
    assert not runtime._shared_runtimes and sorted(state.closed) == [101, 102]


def test_failed_pin_release_of_poisoned_entry_stays_retained_and_refuses(shared):
    make, state = shared
    owner, victim = make(), make()
    owner.acquire()
    state.recheck_error = "tamper"
    with pytest.raises(runtime.RuntimeUnavailable):
        victim.acquire()
    owner.close()
    state.close_fails.add(102)
    with pytest.raises(runtime.RuntimeUnavailable, match="retained"):
        make().acquire()
    with pytest.raises(runtime.RuntimeUnavailable, match="unsettled"):
        make().acquire()


def test_abandoned_session_releases_its_reference(shared):
    make, _ = shared
    lease = make()
    lease.acquire()
    entry = next(iter(runtime._shared_runtimes.values()))
    assert entry.refs == 1
    del lease
    gc.collect()
    assert entry.refs == 0


def test_concurrent_acquire_validates_once(shared):
    make, state = shared
    leases = [make() for _ in range(8)]
    errors = []

    def run(lease):
        try:
            lease.acquire()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(lease,)) for lease in leases]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not errors and state.validations == 1 and state.rechecks == 7
    for lease in leases:
        lease.close()


def test_explicit_parent_stays_exclusive_by_default(tmp_path):
    assert runtime.RuntimeLease(tmp_path / "p")._shared is False
    assert runtime.RuntimeLease()._shared is True


def test_reuse_records_visible_timings(shared):
    make, _ = shared
    first, second = make(), make()
    recorder = timing.Recorder()
    token = timing.current.set(recorder)
    try:
        first.acquire()
        second.acquire()
    finally:
        timing.current.reset(token)
    snap = recorder.snapshot()
    assert snap["runtime_acquire"]["calls"] == 2
    assert snap["runtime_reuse"]["calls"] == 1 and snap["runtime_recheck"]["calls"] == 1
    first.close()
    second.close()


# ----------------------------------------------------------------------------- native (Windows)

native = pytest.mark.skipif(__import__("os").name != "nt", reason="Windows handles, sharing and ACLs")


def _native_sources(tmp_path):
    base = tmp_path / "original-python"
    (base / "Lib").mkdir(parents=True)
    (base / "python.exe").write_bytes(b"interpreter")
    (base / "Lib" / "os.py").write_bytes(b"stdlib")
    site = tmp_path / "venv-packages"
    site.mkdir()
    (site / "pytest.py").write_bytes(b"pytest")
    return base, site


@pytest.fixture
def native_runtime(tmp_path, monkeypatch):
    base, site = _native_sources(tmp_path)
    parent = tmp_path / "data" / "verification-runtimes"
    hashes = []
    real = runtime.validate_runtime

    def counting(*args, **kwargs):
        hashes.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(runtime, "validate_runtime", counting)
    leases = []

    def make():
        lease = runtime.RuntimeLease(parent, shared=True)
        leases.append(lease)
        return lease

    yield make, base, site, parent, hashes
    for lease in leases:
        try:
            lease.close()
        except runtime.RuntimeUnavailable:
            pass
    runtime.shutdown_shared_runtimes()


@native
def test_handle_security_matches_path_security_for_every_pin(tmp_path):
    from orchestrator.security import acl

    base, site = _native_sources(tmp_path)
    parent = tmp_path / "data" / "verification-runtimes"
    lease = runtime.RuntimeLease(parent, shared=False)
    try:
        lease.acquire(base=base, site_packages=site)
        assert lease._pins
        for handle in lease._pins:
            path = lease._pin_paths[handle]
            assert runtime._read_handle_security(handle) == (sandbox.read_label(path), acl.read_security(path))
    finally:
        lease.close()


@native
def test_native_second_acquire_skips_hashing_and_reports_phase_costs(native_runtime):
    make, base, site, _, hashes = native_runtime
    recorder = timing.Recorder()
    token = timing.current.set(recorder)
    try:
        first, second = make(), make()
        first.acquire(base=base, site_packages=site)
        cold = recorder.snapshot()
        second.acquire()
        warm = recorder.snapshot()
    finally:
        timing.current.reset(token)
    assert len(hashes) == 1
    assert cold["runtime_hash"]["calls"] == 1 and warm["runtime_hash"]["calls"] == 1
    assert warm["runtime_recheck"]["calls"] == 1 and "runtime_recheck" not in cold
    assert warm["runtime_pin"]["calls"] == 1
    print("NATIVE-TIMING cold", cold)
    print("NATIVE-TIMING warm", warm)
    first.close()
    second.close()


@native
def test_native_dacl_tamper_between_acquires_is_refused_and_poisons(native_runtime):
    make, base, site, parent, hashes = native_runtime
    from orchestrator.security import acl

    first, second, third = make(), make(), make()
    exe = first.acquire(base=base, site_packages=site)
    target = exe.parent / "Lib" / "os.py"
    acl._icacls([str(target), "/grant", "*S-1-1-0:(R)"])  # Everyone: an unexpected principal
    with pytest.raises(runtime.RuntimeUnavailable, match="unexpected principal"):
        second.acquire()
    with pytest.raises(runtime.RuntimeUnavailable, match="not reused"):
        third.acquire()
    assert len(hashes) == 1
    first.close()


@native
def test_native_file_added_to_pinned_directory_is_refused(native_runtime):
    make, base, site, _, _ = native_runtime
    first, second = make(), make()
    exe = first.acquire(base=base, site_packages=site)
    (exe.parent / "Lib" / "planted.pth").write_text("import evil")
    with pytest.raises(runtime.RuntimeUnavailable, match="file set changed"):
        second.acquire()
    first.close()


@native
def test_native_attribute_change_on_a_pinned_file_is_refused(native_runtime):
    import os
    import stat

    make, base, site, _, _ = native_runtime
    first, second, third = make(), make(), make()
    exe = first.acquire(base=base, site_packages=site)
    target = exe.parent / "Lib" / "os.py"
    os.chmod(target, stat.S_IREAD)  # FILE_ATTRIBUTE_READONLY: allowed by the pins' share mode
    with pytest.raises(runtime.RuntimeUnavailable, match="attributes, identity or link count changed"):
        second.acquire()
    first.close()
    runtime.shutdown_shared_runtimes()
    os.chmod(target, stat.S_IREAD | stat.S_IWRITE)
    # A second name for a pinned file is already impossible (CreateHardLink hits the pins' sharing mode); the
    # link count is compared anyway in case another path to it exists.
    first = make()
    exe = first.acquire(base=base, site_packages=site)
    with pytest.raises(PermissionError):
        os.link(target, exe.parent.parent / "outside-link.py")
    third.acquire()
    third.close()
    first.close()


def test_refs_lock_is_reentrant_for_a_finalizer_run_inside_it():
    """LOW-3: weakref.finalize -> release_ref may run (cyclic GC) on a thread already holding refs_lock."""
    entry = runtime._SharedRuntime(core=SimpleNamespace())
    entry.refs = 2
    done = threading.Event()

    def inside_increment():
        with entry.refs_lock:
            entry.refs += 1
            entry.release_ref()  # what a GC-triggered finalizer would do here
        done.set()

    worker = threading.Thread(target=inside_increment, daemon=True)
    worker.start()
    assert done.wait(5), "release_ref self-deadlocked inside refs_lock"
    assert entry.refs == 2
