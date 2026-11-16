"""Stage a private Python runtime without changing the source installation or its ACLs.

The caller supplies an orchestrator-owned, Medium-integrity location outside task
worktrees. AppContainer access is granted to this copy only, by the sandbox layer.
No download, package installation, or modification of the user's interpreter occurs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sys
import threading
import weakref
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path, PureWindowsPath

from orchestrator.security import acl, paths, timing

log = logging.getLogger(__name__)


class RuntimeUnavailable(RuntimeError):
    pass


def _incomplete_staging(destination: Path) -> bool:
    """True for a plain (non-link) directory without runtime-manifest.json: staging writes the manifest last,
    so this is a copy whose staging was interrupted. Links, files and complete copies are never matched."""
    try:
        st = os.lstat(destination)
    except FileNotFoundError:
        return False
    import stat as _stat

    if not _stat.S_ISDIR(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:
        return False
    return not os.path.lexists(destination / "runtime-manifest.json")


def _runtime_relative_parts(name: str) -> list[str]:
    try:
        parts = paths._rel_parts(name)
    except paths.PathPolicyError as exc:
        raise RuntimeUnavailable("unsafe runtime relative path") from exc
    if any(part != part.rstrip(" .") or PureWindowsPath(part).is_reserved()
           or any(ord(char) < 32 or char in '<>:"|?*' for char in part) for part in parts):
        raise RuntimeUnavailable("unsafe runtime relative path")
    return parts


def _plain_tree(source: Path, *, skip_cache: bool = True):
    """Enumerate trusted source files, refusing links instead of copying their targets."""
    stack = [source]
    while stack:
        parent = stack.pop()
        for entry in parent.iterdir():
            st = entry.lstat()
            if entry.is_symlink() or getattr(st, "st_file_attributes", 0) & 0x400:
                raise RuntimeUnavailable(f"runtime source contains a link: {entry}")
            if entry.is_dir():
                if not skip_cache or entry.name != "__pycache__":
                    stack.append(entry)
            elif entry.is_file():
                yield entry


def _stage_python(destination: Path, *, base: Path | None = None, site_packages: Path | None = None,
                  appcontainer_read: bool = False) -> Path:
    """Copy CPython and installed dependencies to a NEW destination; never reuse an unverified tree.

    Deliberately refuses an existing destination. A publisher can validate this
    staged tree and atomically rename it into its protected runtime location.
    """
    destination = Path(destination).absolute()
    base = Path(base or sys.base_prefix).resolve()
    site_packages = Path(site_packages or Path(sys.prefix) / "Lib" / "site-packages").resolve()
    for source in (base, site_packages):
        if not source.is_dir():
            raise RuntimeUnavailable(f"missing Python source directory: {source}")
        if destination == source or source in destination.parents or destination in source.parents:
            raise RuntimeUnavailable("runtime destination overlaps its source installation")
    if os.path.lexists(destination):
        raise RuntimeUnavailable("runtime staging destination already exists")
    if not destination.parent.is_dir() or destination.parent.is_symlink():
        raise RuntimeUnavailable("runtime staging parent must be an existing private directory")
    files = []
    # Enumerate before creating anything: unsupported source links fail without
    # leaving a partially usable interpreter behind.
    for source in _plain_tree(base):
        rel = source.relative_to(base)
        if "site-packages" not in rel.parts and source.suffix.casefold() not in {".pth", "._pth"}:
            files.append((source, rel))
    for source in _plain_tree(site_packages):
        # Editable install path hooks can refer back to the orchestrator checkout.
        if source.suffix.casefold() not in {".pth", ".pyc"}:
            files.append((source, Path("Lib/site-packages") / source.relative_to(site_packages)))
    if not any(rel == Path("python.exe") for _, rel in files):
        raise RuntimeUnavailable("source runtime has no python.exe")
    return stage_runtime_files(destination, files, required_executable="python.exe", appcontainer_read=appcontainer_read)


@timing.timed("runtime_stage")
def stage_runtime_files(destination: Path, files, *, required_executable: str,
                        appcontainer_read: bool = False, generated: dict[str, bytes] | None = None) -> Path:
    """Publish content into a NEW private tree; callers guard/enumerate trusted sources.

    Shared by the Python and Node stagers. Never transplant source ACLs, modify
    an existing destination, or return a partially copied runtime.
    """
    destination = Path(destination).absolute()
    if os.path.lexists(destination):
        raise RuntimeUnavailable("runtime staging destination already exists")
    if not destination.parent.is_dir() or destination.parent.is_symlink():
        raise RuntimeUnavailable("runtime staging parent must be an existing private directory")
    files = list(files)
    generated = generated or {}
    names = [Path(rel).as_posix() for _, rel in files] + list(generated)
    if len(set(names)) != len(names) or required_executable not in names:
        raise RuntimeUnavailable("runtime file set is duplicate or missing its executable")
    for name in names:
        _runtime_relative_parts(name)  # reject Windows aliases before any creation
        if name.casefold() == "runtime-manifest.json":
            raise RuntimeUnavailable("unsafe runtime relative path")
    with paths.directory_guard(destination.parent):
        return _copy_runtime_files(destination, files, generated, required_executable, appcontainer_read)


def _copy_runtime_files(destination, files, generated, required_executable, appcontainer_read):
    destination.mkdir()
    if not acl.harden_private_dir(destination) or acl.check_private(destination):
        raise RuntimeUnavailable("cannot protect the new runtime copy with a private ACL")
    if appcontainer_read:
        from orchestrator.security import appcontainer

        # This immutable copy contains no secrets. Sharing RX avoids per-task ACE
        # accumulation; task-specific worktrees and scratch never use this group.
        appcontainer.grant_directory(destination, "S-1-15-2-1", writable=False)
    hashes = {}
    try:
        for source, rel in files:
            target = destination / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)  # content only: never transplant source ACLs
            hashes[rel.as_posix()] = hashlib.sha256(target.read_bytes()).hexdigest()
        for rel, content in generated.items():
            target = destination / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            hashes[Path(rel).as_posix()] = hashlib.sha256(content).hexdigest()
        (destination / "runtime-manifest.json").write_text(
            json.dumps({"version": 1, "files": hashes}, sort_keys=True), encoding="utf-8"
        )
    except (OSError, RuntimeUnavailable) as exc:
        # No automatic recursive removal here. A partially staged copy is never
        # published or returned, and can be inspected by the operator.
        raise RuntimeUnavailable(f"runtime staging failed: {exc}") from exc
    return destination / required_executable


def stage_python(destination: Path, *, base: Path | None = None, site_packages: Path | None = None,
                 appcontainer_read: bool = False) -> Path:
    """Stage a new, owner-only copy while holding its ancestor chain against replacement.

    Only the newly created destination receives an ACL; never the source runtime
    or an existing parent. This helper does not activate or execute the copy.
    """
    destination = Path(destination).absolute()
    try:
        with paths.directory_guard(destination.parent):
            return _stage_python(destination, base=base, site_packages=site_packages,
                                 appcontainer_read=appcontainer_read)
    except (OSError, paths.PathPolicyError) as exc:
        raise RuntimeUnavailable(f"unsafe or inaccessible runtime staging location: {exc}") from exc


_runtime_lock = threading.Lock()
_retained_runtime_leases: set[RuntimeLease] = set()


class _SharedRuntime:
    """Process-wide, pinned and fully validated runtime shared by every session of one managed location.

    `core` is an exclusive lease that did the full pin + hash + ACL validation once. While its pins are held
    file content cannot change (no write/delete sharing), so later sessions only re-check what pins cannot
    prevent: DACL/owner/label changes (re-read on the held handles) and entries added to pinned directories.
    Pins stay until shutdown_shared_runtimes() or until the entry is poisoned and unreferenced.

    Lock order: `_runtime_lock` -> `refs_lock` (never the reverse). `refs_lock` is re-entrant: the
    weakref.finalize of an abandoned lease calls release_ref() and may be run by cyclic garbage collection on a
    thread that already holds `refs_lock` (inside release_ref or the increment in _acquire_shared); a plain Lock
    would self-deadlock there. release_ref never takes `_runtime_lock`.
    """

    def __init__(self, core: RuntimeLease):
        self.core = core
        self.poisoned = False
        self.refs = 0
        self.refs_lock = threading.RLock()

    def release_ref(self) -> None:
        with self.refs_lock:
            self.refs = max(0, self.refs - 1)


_shared_runtimes: dict[tuple[str, str], _SharedRuntime] = {}


def _shared_key(parent: Path, runtime_name: str) -> tuple[str, str]:
    return os.path.normcase(str(parent)), runtime_name.casefold()


def _pin_identity(handle: int) -> tuple[int, int, int, int, int]:
    """(attributes, volume serial, file index high, file index low, link count) of a held runtime object.
    Pins deny write/delete sharing but not attribute changes or new hardlinks (neither needs a shared mode)."""
    import ctypes

    from orchestrator.security import sandbox as s

    info = s._BY_HANDLE_FILE_INFORMATION()
    if not s._k32.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise s._winerr("GetFileInformationByHandle(runtime)")
    return (info.dwFileAttributes, info.dwVolumeSerialNumber, info.nFileIndexHigh, info.nFileIndexLow,
            info.nNumberOfLinks)


def _recheck_pins(core: RuntimeLease) -> None:
    """Cheap per-acquire re-validation of a shared runtime; raises RuntimeUnavailable on any difference."""
    try:
        for directory, names in list(core._listing.items()):
            if frozenset(os.listdir(directory)) != names:
                raise RuntimeUnavailable(f"runtime file set changed: {directory}")
        trusted = {acl.current_user_sid(), acl.SID_SYSTEM, acl.SID_ADMINISTRATORS}
        for handle in list(core._pins):
            if _pin_identity(handle) != core._pin_ids.get(handle):
                raise RuntimeUnavailable("runtime object attributes, identity or link count changed: "
                                         f"{core._pin_paths.get(handle, '<pinned object>')}")
            label, security = _read_handle_security(handle)
            _check_object_security(core._pin_paths.get(handle, "<pinned object>"), label, security, trusted)
        # The root itself is held by a directory guard, not a numeric pin.
        from orchestrator.security import sandbox

        root = core.parent / core.runtime_name
        _check_object_security(root, sandbox.read_label(root), acl.read_security(root), trusted)
    except (OSError, ValueError, paths.PathPolicyError) as exc:
        raise RuntimeUnavailable(f"runtime re-check failed: {exc}") from exc


def shutdown_shared_runtimes() -> int:
    """Release the pins of every unreferenced shared runtime; returns how many remain (in use or unsettled)."""
    with timing.lock_wait(_runtime_lock):
        for key, entry in list(_shared_runtimes.items()):
            if entry.refs == 0:
                entry.poisoned = True
                try:
                    entry.core._close_locked()
                except RuntimeUnavailable:
                    continue  # retained by the core's own veto
                del _shared_runtimes[key]
        return len(_shared_runtimes)


def managed_runtime_parent() -> Path:
    return Path(__file__).resolve().parents[3] / "data" / "verification-runtimes"


class RuntimeLease:
    """Keep validated runtime bytes and their entire path stable until job exit.

    Existing managed parents are checked, never repaired. The legacy staging
    directory is neither adopted nor relabelled. Handles deny write/delete sharing
    on every runtime object, including modules loaded long after process startup.
    """

    def __init__(self, parent: Path | None = None, *, runtime_name: str = "python-v2",
                 required_executable: str = "python.exe", stage: Callable[[Path], Path] | None = None,
                 shared: bool | None = None):
        if any(not name or Path(name).name != name or name in {".", ".."}
               for name in (runtime_name, required_executable)):
            raise RuntimeUnavailable("runtime names must be plain path components")
        for name in (runtime_name, required_executable):
            _runtime_relative_parts(name)
        self.parent = Path(parent or managed_runtime_parent()).absolute()
        self.runtime_name = runtime_name
        self.required_executable = required_executable
        self._stage = stage
        self._held = ExitStack()
        self._pins: list[int] = []
        self._pending_pin: int | None = None
        self._cleanup_failed = False
        self.executable: Path | None = None
        # Only the managed production location is shared by default; an explicit parent stays exclusive.
        self._shared = parent is None if shared is None else shared
        self._entry: _SharedRuntime | None = None
        self._finalizer: Callable[[], object] = lambda: None
        self._listing: dict[Path, frozenset[str]] = {}
        self._pin_paths: dict[int, str] = {}
        self._pin_ids: dict[int, tuple[int, int, int, int, int]] = {}  # first-acquire snapshot (_pin_identity)

    @timing.timed("runtime_pin")
    def _hold_tree(self, root: Path) -> None:
        from orchestrator.security import sandbox as s

        stack = [root]
        while stack:
            directory = stack.pop()
            entries = list(directory.iterdir())
            self._listing[directory] = frozenset(entry.name for entry in entries)
            for entry in entries:
                self._pending_pin = s._k32.CreateFileW(str(entry), 0x80000000 | 0x20000, 1, None, 3,
                                                     0x02000000 | 0x00200000, None)
                handle = self._pending_pin
                if not handle or handle == s.INVALID_HANDLE_VALUE:
                    self._pending_pin = None
                    raise s._winerr(f"lease runtime object {entry}")
                self._pins.append(handle)
                self._pending_pin = None
                self._pin_paths[handle] = str(entry)
                identity = _pin_identity(handle)
                self._pin_ids[handle] = identity
                attributes, links = identity[0], identity[4]
                if attributes & 0x400:
                    raise RuntimeUnavailable(f"runtime object is a reparse point: {entry}")
                if attributes & 0x10:
                    stack.append(entry)
                elif links != 1:
                    raise RuntimeUnavailable(f"runtime file has more than one link: {entry}")

    @timing.timed("runtime_acquire")
    def acquire(self, *, base: Path | None = None, site_packages: Path | None = None) -> Path:
        if self._shared:
            return self._acquire_shared(base, site_packages)
        with timing.lock_wait(_runtime_lock):
            return self._acquire_locked(base, site_packages)

    def _acquire_shared(self, base: Path | None, site_packages: Path | None) -> Path:
        key = _shared_key(self.parent, self.runtime_name)
        with timing.lock_wait(_runtime_lock):
            if self._entry is not None and not self._entry.poisoned:
                return self.executable  # idempotent, like the exclusive lease
            if self._cleanup_failed or any(
                lease.parent / lease.runtime_name == self.parent / self.runtime_name
                for lease in _retained_runtime_leases
            ):
                raise RuntimeUnavailable("runtime cleanup is unsettled; lease retained until process restart")
            entry = _shared_runtimes.get(key)
            if entry is not None and entry.poisoned:
                if entry.refs:
                    raise RuntimeUnavailable("shared runtime failed validation; it is not reused")
                # Unreferenced poisoned entry: release its pins; a failed release stays retained and refuses.
                entry.core._close_locked()
                del _shared_runtimes[key]
                entry = None
            if entry is None:
                core = RuntimeLease(self.parent, runtime_name=self.runtime_name,
                                    required_executable=self.required_executable, stage=self._stage, shared=False)
                core._acquire_locked(base, site_packages)  # full pin + hash + ACL, exactly as an exclusive lease
                entry = _shared_runtimes[key] = _SharedRuntime(core)
                reused = False
            else:
                reused = True
            with entry.refs_lock:
                entry.refs += 1
            self._entry = entry
            self.executable = entry.core.executable
            # Released exactly once: by close(), or by garbage collection of an abandoned session.
            self._finalizer = weakref.finalize(self, entry.release_ref)
        if reused:
            try:
                with timing.measure("runtime_reuse"):
                    with timing.measure("runtime_recheck"):
                        _recheck_pins(entry.core)
            except BaseException:
                with _runtime_lock:
                    entry.poisoned = True  # never reused again
                    self._entry = None
                    self.executable = None
                    self._finalizer()
                raise
        return self.executable

    def _acquire_locked(self, base: Path | None, site_packages: Path | None) -> Path:
        from orchestrator.security import appcontainer

        # A refused request must never release a pre-existing owner's pins.
        if self._cleanup_failed or any(
            lease.parent / lease.runtime_name == self.parent / self.runtime_name
            for lease in _retained_runtime_leases
        ):
            raise RuntimeUnavailable("runtime cleanup is unsettled; lease retained until process restart")
        if self.executable is not None:
            return self.executable
        try:
            # Pin weak existing ancestors before inspecting or creating our
            # managed parent. Only directories created here receive new ACLs.
            self._held.enter_context(paths.directory_guard(self.parent.parent.parent))
            if not os.path.lexists(self.parent.parent):
                self.parent.parent.mkdir()
                if not acl.harden_private_dir(self.parent.parent):
                    raise RuntimeUnavailable("cannot protect newly created data parent")
            self._held.enter_context(paths.directory_guard(self.parent.parent))
            if not os.path.lexists(self.parent):
                self.parent.mkdir()
                if not acl.harden_private_dir(self.parent):
                    raise RuntimeUnavailable("cannot protect new managed runtime parent")
            self._held.enter_context(paths.directory_guard(self.parent))
            if acl.check_private(self.parent):
                raise RuntimeUnavailable("existing managed runtime parent is not private and trusted")
            destination = self.parent / self.runtime_name
            if _incomplete_staging(destination):
                # Staging writes runtime-manifest.json last, so a plain directory without it is a copy whose
                # staging was interrupted (process stopped during the first, minute-long staging). It was never
                # published or executed. Remove it without following links (inside the private, held parent)
                # and stage again; any removal problem fails closed.
                from orchestrator.security import sandbox as s

                log.warning("removing an incompletely staged runtime copy before restaging: %s", destination)
                s.remove_tree(destination)
            if not os.path.lexists(destination):
                if self._stage is not None:
                    self._stage(destination)
                else:
                    stage_python(destination, base=base, site_packages=site_packages, appcontainer_read=True)
            self._held.enter_context(paths.directory_guard(destination))
            self._hold_tree(destination)
            self.executable = validate_runtime(destination, check_security=True,
                                               required_executable=self.required_executable)
            return self.executable
        except (OSError, paths.PathPolicyError, RuntimeUnavailable, appcontainer.AppContainerUnavailable) as exc:
            self._close_locked()
            raise RuntimeUnavailable(f"cannot lease private runtime: {exc}") from exc
        except BaseException:
            try:
                self._close_locked()
            except Exception:  # noqa: BLE001, S110 - state retained; preserve interruption, do not log native context
                pass
            raise

    def close(self) -> None:
        """Keep guards and ownership on uncertain close; never retry a failed numeric handle.

        A shared lease only drops its reference: the pins belong to the process-wide runtime."""
        with _runtime_lock:
            if self._entry is not None:
                self._entry = None
                self.executable = None
                self._finalizer()
                return
            self._close_locked()

    def _close_locked(self) -> None:
        """Caller holds _runtime_lock; shared with acquisition error cleanup."""
        from orchestrator.security import sandbox as s

        if self._cleanup_failed:
            raise RuntimeUnavailable("runtime cleanup failed; lease retained until process restart")
        try:
            if self._pending_pin is not None:
                # An interruption can occur after append but before clearing the pending slot.
                if not self._pins or self._pins[-1] != self._pending_pin:  # noqa: SIM102 - distinguish ownership from close
                    if not s._k32.CloseHandle(self._pending_pin):
                        raise RuntimeUnavailable("cannot release unregistered runtime file handle")
                self._pending_pin = None
            while self._pins:
                if not s._k32.CloseHandle(self._pins[-1]):
                    raise RuntimeUnavailable("cannot release runtime file handle")
                self._pins.pop()
            self._held.close()
        except BaseException as exc:
            self._cleanup_failed = True
            _retained_runtime_leases.add(self)  # prevent guard generators finalizing on caller abandonment
            if isinstance(exc, Exception):
                raise RuntimeUnavailable("runtime cleanup failed; lease retained until process restart") from None
            raise
        self.executable = None
        self._listing.clear()
        self._pin_paths.clear()
        self._pin_ids.clear()
        _retained_runtime_leases.discard(self)


def _check_object_security(p, label, security, trusted) -> None:
    """Fail-closed policy for one runtime object; shared by path-based and handle-based checks."""
    from orchestrator.security import sandbox

    # Every copied object must remain Medium or higher; a Low process must
    # never be able to rewrite ACLs as its owner.
    if label is not None and (label[0] < sandbox.MEDIUM_RID or not label[1] & sandbox.NO_WRITE_UP):
        raise RuntimeUnavailable(f"runtime integrity changed: {p}")
    if security["owner"] not in trusted or security["aces"] is None:
        raise RuntimeUnavailable(f"runtime ownership changed: {p}")
    package_rx = False
    for ace in security["aces"]:
        if ace["type"] != 0:
            continue
        if ace["sid"] == "S-1-15-2-1":
            if ace["mask"] & ~0x1200a9:
                raise RuntimeUnavailable(f"runtime grants package write access: {p}")
            package_rx |= ace["mask"] & 0x1200a9 == 0x1200a9
        elif ace["sid"] not in trusted:
            raise RuntimeUnavailable(f"runtime grants an unexpected principal access: {p}")
    if not package_rx:
        raise RuntimeUnavailable(f"runtime has no package RX grant: {p}")


_handle_advapi_cache = None


def _handle_advapi():
    global _handle_advapi_cache
    if _handle_advapi_cache is None:
        import ctypes
        from ctypes import wintypes

        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        adv.GetSecurityInfo.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p,
                                        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        adv.GetSecurityInfo.restype = wintypes.DWORD
        _handle_advapi_cache = adv
    return _handle_advapi_cache


def _read_handle_security(handle: int) -> tuple[tuple[int, int, int] | None, dict]:
    """(label, security) of an already-open object through its handle: no path is re-opened.

    One GetSecurityInfo call returns owner, DACL and mandatory label (READ_CONTROL is enough). The result has
    the same shape as sandbox.read_label / acl.read_security."""
    import ctypes
    from ctypes import wintypes

    from orchestrator.security import sandbox as s

    powner, pdacl, psacl, psd = (ctypes.c_void_p() for _ in range(4))
    err = _handle_advapi().GetSecurityInfo(
        ctypes.c_void_p(handle), 1, 0x1 | 0x4 | s.LABEL_SECURITY_INFORMATION,
        ctypes.byref(powner), None, ctypes.byref(pdacl), ctypes.byref(psacl), ctypes.byref(psd))
    if err != 0:
        raise OSError(err, "GetSecurityInfo failed for a pinned runtime object")
    try:
        label = None
        if psacl.value:
            sacl = acl._ACL.from_address(psacl.value)
            for i in range(sacl.AceCount):
                pace = ctypes.c_void_p()
                if not acl._advapi32.GetAce(psacl, i, ctypes.byref(pace)):
                    raise OSError(ctypes.get_last_error(), "GetAce failed")
                hdr = acl._ACE_HEADER.from_address(pace.value)
                if hdr.AceType == s.SYSTEM_MANDATORY_LABEL_ACE_TYPE:
                    mask = ctypes.c_uint32.from_address(pace.value + 4).value
                    label = (s._sid_rid(pace.value + 8), mask, hdr.AceFlags)
                    break
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if not acl._advapi32.GetSecurityDescriptorControl(psd, ctypes.byref(control), ctypes.byref(revision)):
            raise OSError(ctypes.get_last_error(), "GetSecurityDescriptorControl failed")
        owner = acl._sid_to_str(powner.value) if powner.value else None
        if not pdacl.value:
            return label, {"owner": owner, "protected": bool(control.value & acl._SE_DACL_PROTECTED), "aces": None}
        dacl = acl._ACL.from_address(pdacl.value)
        aces: list[dict] = []
        for i in range(dacl.AceCount):
            pace = ctypes.c_void_p()
            if not acl._advapi32.GetAce(pdacl, i, ctypes.byref(pace)):
                raise OSError(ctypes.get_last_error(), "GetAce failed")
            hdr = acl._ACE_HEADER.from_address(pace.value)
            sid, mask = None, 0
            if hdr.AceType in (acl._ACCESS_ALLOWED_ACE_TYPE, acl._ACCESS_DENIED_ACE_TYPE):
                mask = ctypes.c_uint32.from_address(pace.value + 4).value
                sid = acl._sid_to_str(pace.value + 8)
            aces.append({"type": hdr.AceType, "sid": sid, "mask": mask,
                         "inherited": bool(hdr.AceFlags & acl._INHERITED_ACE), "flags": hdr.AceFlags})
        return label, {"owner": owner, "protected": bool(control.value & acl._SE_DACL_PROTECTED), "aces": aces}
    finally:
        acl._kernel32.LocalFree(psd)


@timing.timed("runtime_validate")
def validate_runtime(destination: Path, *, check_security: bool = False,
                     required_executable: str = "python.exe") -> Path:
    """Validate every byte and refuse additional files, links, or Python path hooks."""
    try:
        with paths.directory_guard(destination):
            manifest = json.loads((destination / "runtime-manifest.json").read_text(encoding="utf-8"))
            if manifest.get("version") != 1 or not isinstance(manifest.get("files"), dict):
                raise RuntimeUnavailable("invalid runtime manifest")
            actual = {p.relative_to(destination).as_posix(): p for p in _plain_tree(destination, skip_cache=False)
                      if p != destination / "runtime-manifest.json"}
            if set(actual) != set(manifest["files"]):
                raise RuntimeUnavailable("runtime file set changed")
            with timing.measure("runtime_hash"):
                for rel, p in actual.items():
                    if p.suffix.casefold() in {".pth", "._pth", ".pyc"}:
                        raise RuntimeUnavailable("runtime contains a Python path hook or bytecode")
                    if hashlib.sha256(p.read_bytes()).hexdigest() != manifest["files"][rel]:
                        raise RuntimeUnavailable(f"runtime content changed: {rel}")
            if required_executable not in actual:
                raise RuntimeUnavailable("runtime has no required executable")
            if check_security:
                from orchestrator.security import sandbox

                with timing.measure("runtime_acl"):
                    trusted = {acl.current_user_sid(), acl.SID_SYSTEM, acl.SID_ADMINISTRATORS}
                    objects = [destination, *(p for p in destination.rglob("*"))]
                    for p in objects:
                        _check_object_security(p, sandbox.read_label(p), acl.read_security(p), trusted)
            return destination / required_executable
    except (OSError, ValueError, paths.PathPolicyError) as exc:
        raise RuntimeUnavailable(f"runtime validation failed: {exc}") from exc


_BOOTSTRAP = r'''
import json, os, runpy, sys
environment = json.loads(sys.argv[1])
os.environ.clear()
os.environ.update(environment)
# CPython's Windows 0700 DACL excludes the AppContainer itself. Create this
# particular mode with a protected user/SYSTEM/exact-package DACL in private
# scratch, then move the empty directory atomically to its requested location.
# Windows requires WRITE_DAC on the creation parent for an explicit descriptor;
# only new per-command scratch has that right. Worktree roots remain pinned.
# Never weaken to inherited permissions, overwrite a target, or copy across disks.
original_mkdir = os.mkdir
def task_mkdir(path, mode=0o777, *, dir_fd=None):
    if mode != 0o700 or dir_fd is not None:
        return original_mkdir(path, mode, dir_fd=dir_fd)
    import ctypes
    from ctypes import wintypes as w
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    a = ctypes.WinDLL("advapi32", use_last_error=True)
    k.GetCurrentProcess.restype = w.HANDLE
    k.CloseHandle.argtypes = [w.HANDLE]
    k.LocalFree.argtypes = [ctypes.c_void_p]
    k.CreateDirectoryW.argtypes = [w.LPCWSTR, ctypes.c_void_p]
    k.CreateDirectoryW.restype = w.BOOL
    a.OpenProcessToken.argtypes = [w.HANDLE,w.DWORD,ctypes.POINTER(w.HANDLE)]
    a.GetTokenInformation.argtypes = [w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD,ctypes.POINTER(w.DWORD)]
    a.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,ctypes.POINTER(w.LPWSTR)]
    a.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR,w.DWORD,ctypes.c_void_p,ctypes.c_void_p]
    token = w.HANDLE()
    if not a.OpenProcessToken(k.GetCurrentProcess(),8,ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    def sid_string(info_class):
        size = w.DWORD()
        a.GetTokenInformation(token,info_class,None,0,ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not a.GetTokenInformation(token,info_class,buffer,len(buffer),ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        pointer = ctypes.c_void_p.from_buffer(buffer).value
        value = w.LPWSTR()
        if not pointer or not a.ConvertSidToStringSidW(pointer,ctypes.byref(value)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return value.value
        finally:
            k.LocalFree(ctypes.cast(value,ctypes.c_void_p))
    try:
        user, package = sid_string(1), sid_string(31)
    finally:
        k.CloseHandle(token)
    if not package.startswith("S-1-15-2-"):
        raise PermissionError("private directory requires a task AppContainer SID")
    sd = ctypes.c_void_p()
    sddl = (f"D:P(A;OICI;FA;;;{user})(A;OICI;FA;;;SY)"
            f"(A;OICI;FA;;;{package})")
    if not a.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl,1,ctypes.byref(sd),None):
        raise ctypes.WinError(ctypes.get_last_error())
    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("length",w.DWORD),("descriptor",ctypes.c_void_p),("inherit",w.BOOL)]
    try:
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes),sd.value,False)
        destination = os.path.abspath(os.fsdecode(path))
        staging = os.path.join(environment["TEMP"], "private-" + os.urandom(16).hex())
        if not k.CreateDirectoryW(staging,ctypes.byref(attributes)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            os.rename(staging,destination)
        except BaseException:
            os.rmdir(staging)  # newly created empty object only; never follow links
            raise
    finally:
        k.LocalFree(sd)
os.mkdir = task_mkdir
args = sys.argv[2:]
if args[:2] == ["-m", "pytest"]:
    from pathlib import Path
    from _pytest.config import PytestPluginManager
    original = PytestPluginManager._getconftestmodules
    boundary = Path.cwd()
    def task_conftests(manager, path):
        # Pytest queries its initial collector's parent before applying confcutdir.
        # Outside the task there are deliberately no applicable conftests. Avoid
        # the forbidden metadata probe, preserving all hooks inside the task.
        candidate = Path(path).absolute()
        if not candidate.is_relative_to(boundary):
            return ()
        return original(manager, path)
    PytestPluginManager._getconftestmodules = task_conftests
if args[:1] == ["-m"] and len(args) >= 2:
    sys.argv = args[1:]
    runpy.run_module(args[1], run_name="__main__", alter_sys=True)
elif args[:1] == ["-c"] and len(args) >= 2:
    sys.argv = ["-c", *args[2:]]
    exec(compile(args[1], "<string>", "exec"), {"__name__": "__main__", "__builtins__": __builtins__})
elif args and not args[0].startswith("-"):
    sys.argv = args
    sys.path.insert(0, os.path.dirname(os.path.abspath(args[0])))
    runpy.run_path(args[0], run_name="__main__")
else:
    raise SystemExit("unsupported private Python invocation")
'''


def verification_argv(argv, *, cwd: str, env: dict[str, str], lease: RuntimeLease | None = None) -> list[str]:
    """Route Python to its leased copy; leave resolved native tools to OS access checks."""
    if not argv or Path(argv[0]).name.casefold() not in {"python", "python.exe", "python3", "python3.exe"}:
        return list(argv)
    if lease is None:
        raise RuntimeUnavailable("Python verification requires a session runtime lease")
    exe = lease.acquire()
    args = list(argv[1:])
    if args[:2] == ["-m", "pytest"]:
        # Pytest otherwise probes parent conftest/config files, correctly denied
        # by the task SID. Restrict discovery rather than granting parent reads.
        args += ["--rootdir=" + cwd, "--confcutdir=" + cwd, "-p", "no:cacheprovider"]
    # Project imports are intentional: agent-written code executes inside the
    # AppContainer. Ignore environment and user site, but preserve script/cwd.
    # Windows rewrites TEMP/LOCALAPPDATA while creating an AppContainer process.
    # Restore our private scratch paths in-process before executing project code.
    return [str(exe), "-E", "-s", "-B", "-c", _BOOTSTRAP, json.dumps(env), *args]
