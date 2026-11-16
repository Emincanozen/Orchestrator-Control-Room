"""Disposable copy-in tree for one sandboxed verification command (security finding N2).

Agent-written code never runs in, and its AppContainer identity is never granted on, the real task worktree.
For every command the orchestrator builds a private copy and runs the command there:

    <project sandbox root>/runs/                 Medium, protected DACL (user, SYSTEM, Administrators only)
        <random>/                                Medium, plain, orchestrator-owned ("base"; Session root)
            tree/                                the copy: AppContainer grant + Low label + pin ("path"; cwd)
            tmp/<random>/  cache-<identity>/     per-command scratch and tool caches (created by Session)

Why this closes the post-launch alias and scan-to-use classes by construction rather than by a scan:

* The grant, label and pin are applied to ``tree/`` while it is EMPTY and before the AppContainer profile of
  this run exists (the profile name is derived from the random copy path, so its SID has never been held by any
  process). ``runs/`` is protected (no inheritance from the drive), so no package-wide ACE (ALL APPLICATION
  PACKAGES / ALL RESTRICTED APPLICATION PACKAGES) lets a concurrent run reach this copy; a run directory or copy
  that still carries such a write grant is refused.
* Every source object is read through one handle opened without following reparse points; type, link count
  and containment are checked ON THAT HANDLE and the bytes are read from it (no check-then-reopen). Each source
  directory is held open without delete/write sharing while it is enumerated, below a handle-pinned ancestor
  chain. Only the unnamed data stream is copied (no alternate streams, no security descriptor, no attributes).
* Every destination object is created exclusively (``FILE_CREATE``) RELATIVE TO A HELD HANDLE of its parent
  directory (``NtCreateFile`` with ``RootDirectory``), never by path; parents are held without delete sharing
  until their subtree is complete, so no rename or junction swap can redirect a write. Timestamps are set on
  the created file's own handle. A created file has exactly one link; nothing in the copy is a reparse point.
* Whatever the sandboxed code creates afterwards (junctions, hardlinks, files) exists only below ``base``. No
  Medium actor ever consumes it: orchestrator git, provider runs, commits and later verifications use the real
  worktree, which the sandbox could not write; the copy is removed without following links and never reused.

Removal (``destroy``, ``sweep``): every entry is opened relative to its held parent directory without following
reparse points and deleted through that handle (``FileDispositionInfoEx``: POSIX semantics, read-only attribute
ignored), so read-only files, links and paths deeper than MAX_PATH are removed; a link is removed as a link. A
directory whose DACL the command changed is reset through the owner's implicit WRITE_DAC (never on a name of a
multi-link file) and then removed. Nothing below a project root is ever removed by path: the root, ``runs/`` and
``residue/`` are held without delete sharing and every run directory is opened relative to them. A project root
carrying a Low label or a write grant for a sandbox-reachable SID is not swept at all (a legacy layout a Low
process could steer). What still cannot be removed is RESIDUE, not tamper evidence: the run directory is held, is
not a reparse point, keeps its file identity and lies in the Medium ``runs/`` directory that no sandbox can write,
and no process of the command survives (job drained). Residue is logged and the run directory is renamed (by
handle) into the project's protected ``residue/`` area, which the per-command sweep does not touch; a
low-frequency sweep retries it with a per-object (file id) backoff. It never blocks a task verdict. A replaced run
directory is reported as tamper; a concurrent removal by another orchestrator process is residue/retry.

Nothing is copied back. A verification command cannot change the worktree; file checks and commits see only
what the provider run left there.
"""

from __future__ import annotations

import ctypes
import logging
import os
import stat as stat_mod
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

from orchestrator.security import paths, sandbox

log = logging.getLogger(__name__)

# Fail closed above these bounds: the command is not run (`sandbox_unavailable:`).
MAX_ENTRIES = 200_000
MAX_BYTES = 2 * 1024 * 1024 * 1024
MAX_DEPTH = 64
_CHUNK = 1024 * 1024
_GITLINK_BYTES = 65536

# Removal bounds. What a command writes is not limited by the copy bounds; above these the rest is residue.
REMOVE_MAX_ENTRIES = 5_000_000
REMOVE_MAX_DEPTH = 20_000  # directory handles held at once while emptying nested directories
SWEEP_MAX_ENTRIES = 16  # run / legacy directories inspected per sweep call
SWEEP_SECONDS = 10.0
DESTROY_SECONDS = 60.0  # bound of one run directory's removal; what is left is residue
LIST_MAX_NAMES = 4096  # names read from one held directory per sweep
RESIDUE_DIR = "residue"
RESIDUE_SCAN_SECONDS = 300.0  # residue/ of one project root is inspected at most this often per process
RESIDUE_BACKOFF_FIRST = 60.0  # per residue object (file id): 60 s, doubling, capped
RESIDUE_BACKOFF_MAX = 3600.0

PACKAGE_WIDE_SIDS = frozenset({"S-1-15-2-1", "S-1-15-2-2"})
# FILE_WRITE_DATA | APPEND | WRITE_EA | DELETE_CHILD | WRITE_ATTRIBUTES | DELETE | WRITE_DAC | WRITE_OWNER
# | GENERIC_ALL | GENERIC_WRITE | MAXIMUM_ALLOWED
_WRITE_RIGHTS = 0x2 | 0x4 | 0x10 | 0x40 | 0x100 | 0x10000 | 0x40000 | 0x80000 | 0x10000000 | 0x40000000 | 0x02000000

_LIVE_LOCK = threading.Lock()
_LIVE: set[str] = set()  # run directories owned by a Session of this process (never swept)
_SWEPT_ONCE: set[str] = set()
_BACKOFF: dict[tuple[int, int, int], tuple[int, float]] = {}  # file id -> (failed attempts, next attempt due)
_RESIDUE_NEXT_SCAN: dict[str, float] = {}  # project root key -> monotonic time of the next residue/ scan
_UNTRUSTED_WARNED: set[str] = set()


class ResidueRetained(OSError):
    """Cleanup could not complete, but the run directory is provably contained (see the module docstring).
    Recorded and retried; not a security finding."""


def _key(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def is_live(path: str | os.PathLike[str]) -> bool:
    with _LIVE_LOCK:
        return _key(path) in _LIVE


def package_write_aces(aces: Optional[Iterable[dict]]) -> list[dict]:
    """The allow ACEs of `aces` (acl.read_security format) granting a package-wide AppContainer SID any write
    right. None (a NULL DACL) counts as one: everybody may write."""
    if aces is None:
        return [{"sid": "NULL DACL", "mask": 0x1F01FF, "type": 0}]
    return [a for a in aces if a.get("type") == 0 and a.get("sid") in PACKAGE_WIDE_SIDS
            and a.get("mask", 0) & _WRITE_RIGHTS]


def _refuse_package_writable(path: Path) -> None:
    from orchestrator.security import acl

    found = package_write_aces(acl.read_security(path)["aces"])
    if found:
        raise sandbox.SandboxUnavailable(f"{path} grants package-wide AppContainer SIDs write access "
                                         f"({', '.join(sorted({a['sid'] for a in found}))}); refusing to use it")


def _protect_runs(runs: Path) -> None:
    """`runs/` gets a protected DACL (current user, SYSTEM, Administrators; inheritable) so nothing granted
    on an ancestor (e.g. a drive root granting ALL APPLICATION PACKAGES write) reaches a run directory."""
    from orchestrator.security import acl

    def ok() -> bool:
        sec = acl.read_security(runs)
        trusted = {acl.current_user_sid(), acl.SID_SYSTEM, acl.SID_ADMINISTRATORS}
        return (sec["protected"] and sec["aces"] is not None
                and all(a["type"] == 0 and a["sid"] in trusted for a in sec["aces"]))

    if ok():
        return
    user = acl.current_user_sid()
    h = _open_path(str(runs), _READ_CONTROL | _WRITE_DAC | _FILE_READ_ATTRIBUTES, _SHARE_ALL)
    try:
        info = _info(h)
        if info.dwFileAttributes & _REPARSE or not info.dwFileAttributes & _DIRECTORY:
            raise sandbox.SandboxUnavailable(f"{runs} is not a plain directory")
        _set_dacl(h, f"D:P(A;OICI;FA;;;{user})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)")
    finally:
        _close(h)
    if not ok():
        raise sandbox.SandboxUnavailable(f"cannot give {runs} a protected owner-only DACL")


# ------------------------------------------------------------------------------------------ native layer

_DELETE = 0x10000
_READ_CONTROL = 0x20000
_WRITE_DAC = 0x40000
_SYNCHRONIZE = 0x100000
_GENERIC_WRITE = 0x40000000
_FILE_LIST_DIRECTORY = 0x1
_FILE_TRAVERSE = 0x20
_FILE_READ_ATTRIBUTES = 0x80
_FILE_WRITE_ATTRIBUTES = 0x100
_SHARE_READ, _SHARE_WRITE, _SHARE_DELETE = 0x1, 0x2, 0x4
_SHARE_ALL = 0x7
_FILE_OPEN, _FILE_CREATE = 1, 2
_FILE_DIRECTORY_FILE = 0x1
_FILE_SYNCHRONOUS_IO_NONALERT = 0x20
_FILE_NON_DIRECTORY_FILE = 0x40
_FILE_OPEN_FOR_BACKUP_INTENT = 0x4000
_FILE_OPEN_REPARSE_POINT = 0x200000
_OBJ_CASE_INSENSITIVE = 0x40
_READONLY, _DIRECTORY, _REPARSE, _NORMAL = 0x1, 0x10, 0x400, 0x80
_STATUS_ACCESS_DENIED = 0xC0000022
_STATUS_OBJECT_NAME_COLLISION = 0xC0000035
_STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034
_STATUS_OBJECT_PATH_NOT_FOUND = 0xC000003A
_STATUS_NOT_A_DIRECTORY = 0xC0000103
_STATUS_SHARING_VIOLATION = 0xC0000043
_STATUS_DELETE_PENDING = 0xC0000056
_FILE_OPEN_IF = 3
_FileRenameInformation = 10
_ERROR_NO_MORE_FILES = 18
_ERROR_SHARING_VIOLATION = 32
_FileBasicInfo, _FileDispositionInfo, _FileFullDirectoryInfo, _FileDispositionInfoEx = 0, 4, 14, 21
# FILE_DISPOSITION_FLAG_DELETE | POSIX_SEMANTICS | IGNORE_READONLY_ATTRIBUTE
_DISPOSITION_EX_FLAGS = 0x1 | 0x2 | 0x10
_DACL_AND_PROTECT = 0x4 | 0x80000000  # DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION
_EPOCH_AS_FILETIME = 116444736000000000


class NtError(OSError):
    def __init__(self, status: int, what: str):
        self.status = status & 0xFFFFFFFF
        winerror = _nt.RtlNtStatusToDosError(status) if os.name == "nt" else 0
        super().__init__(None, f"{what}: NTSTATUS 0x{self.status:08x}", None, winerror)


if os.name == "nt":
    _nt = ctypes.WinDLL("ntdll")
    _k = ctypes.WinDLL("kernel32", use_last_error=True)
    _a = ctypes.WinDLL("advapi32", use_last_error=True)
    _vp = ctypes.c_void_p

    class _UnicodeString(ctypes.Structure):
        _fields_ = [("Length", ctypes.c_ushort), ("MaximumLength", ctypes.c_ushort), ("Buffer", _vp)]

    class _ObjectAttributes(ctypes.Structure):
        _fields_ = [("Length", ctypes.c_ulong), ("RootDirectory", _vp),
                    ("ObjectName", ctypes.POINTER(_UnicodeString)), ("Attributes", ctypes.c_ulong),
                    ("SecurityDescriptor", _vp), ("SecurityQualityOfService", _vp)]

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = [("Status", _vp), ("Information", ctypes.c_size_t)]

    class _FileRenameHeader(ctypes.Structure):  # FILE_RENAME_INFORMATION without its trailing FileName
        _fields_ = [("ReplaceIfExists", ctypes.c_ubyte), ("RootDirectory", _vp), ("FileNameLength", ctypes.c_ulong)]

    class _FileBasicInformation(ctypes.Structure):
        _fields_ = [("CreationTime", ctypes.c_longlong), ("LastAccessTime", ctypes.c_longlong),
                    ("LastWriteTime", ctypes.c_longlong), ("ChangeTime", ctypes.c_longlong),
                    ("FileAttributes", ctypes.c_ulong)]

    _nt.NtCreateFile.argtypes = [ctypes.POINTER(_vp), ctypes.c_ulong, ctypes.POINTER(_ObjectAttributes),
                                 ctypes.POINTER(_IoStatusBlock), _vp, ctypes.c_ulong, ctypes.c_ulong,
                                 ctypes.c_ulong, ctypes.c_ulong, _vp, ctypes.c_ulong]
    _nt.NtCreateFile.restype = ctypes.c_long
    _nt.NtSetInformationFile.argtypes = [_vp, ctypes.POINTER(_IoStatusBlock), _vp, ctypes.c_ulong, ctypes.c_int]
    _nt.NtSetInformationFile.restype = ctypes.c_long
    _nt.RtlNtStatusToDosError.argtypes = [ctypes.c_long]
    _nt.RtlNtStatusToDosError.restype = ctypes.c_ulong
    _k.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong, _vp, ctypes.c_ulong,
                               ctypes.c_ulong, _vp]
    _k.CreateFileW.restype = _vp
    _k.CloseHandle.argtypes = [_vp]
    _k.GetFileInformationByHandle.argtypes = [_vp, _vp]
    _k.GetFileInformationByHandleEx.argtypes = [_vp, ctypes.c_int, _vp, ctypes.c_ulong]
    _k.SetFileInformationByHandle.argtypes = [_vp, ctypes.c_int, _vp, ctypes.c_ulong]
    _k.SetFileTime.argtypes = [_vp, _vp, _vp, _vp]
    _k.WriteFile.argtypes = [_vp, _vp, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), _vp]
    _k.LocalFree.argtypes = [_vp]
    _k.LocalFree.restype = _vp
    _a.GetKernelObjectSecurity.argtypes = [_vp, ctypes.c_ulong, _vp, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
    _a.SetKernelObjectSecurity.argtypes = [_vp, ctypes.c_ulong, _vp]
    _a.GetSecurityDescriptorOwner.argtypes = [_vp, ctypes.POINTER(_vp), ctypes.POINTER(ctypes.c_int)]
    _a.ConvertSidToStringSidW.argtypes = [_vp, ctypes.POINTER(ctypes.c_wchar_p)]
    _a.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong,
                                                                         ctypes.POINTER(_vp), _vp]
    _INVALID = ctypes.c_void_p(-1).value


def _winerror(what: str) -> OSError:
    err = ctypes.get_last_error()
    return OSError(None, f"{what}: {ctypes.FormatError(err).strip()}", None, err)


def _close(h: Optional[int]) -> None:
    if h:
        _k.CloseHandle(h)


def _encode(name: str) -> bytes:
    return name.encode("utf-16-le", "surrogatepass")


def _display(name: bytes) -> str:
    return name.decode("utf-16-le", "replace")[:120]


def _long(path: str) -> str:
    path = os.path.abspath(path)
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def _nt_open(root: int, name: bytes, access: int, share: int, disposition: int, options: int, *,
             attributes: int = 0, case_insensitive: bool = False, synchronous: bool = True) -> int:
    """Open or create the single component `name` relative to the directory handle `root` (never by path),
    without following a reparse point. `synchronous=False`: no SYNCHRONIZE access (a handle only for security
    descriptor calls, e.g. on an object whose DACL denies everything to its owner). Raises NtError."""
    if not name or b"\\\x00" in [name[i:i + 2] for i in range(0, len(name), 2)] or name in (b".\x00", b".\x00.\x00"):
        raise OSError(f"refusing a non-component name {_display(name)!r}")
    buf = ctypes.create_string_buffer(name, len(name) + 2)
    us = _UnicodeString(len(name), len(name) + 2, ctypes.addressof(buf))
    oa = _ObjectAttributes(ctypes.sizeof(_ObjectAttributes), root, ctypes.pointer(us),
                           _OBJ_CASE_INSENSITIVE if case_insensitive else 0, None, None)
    handle, iosb = _vp(), _IoStatusBlock()
    sync_access, sync_option = (_SYNCHRONIZE, _FILE_SYNCHRONOUS_IO_NONALERT) if synchronous else (0, 0)
    status = _nt.NtCreateFile(ctypes.byref(handle), access | sync_access, ctypes.byref(oa), ctypes.byref(iosb),
                              None, attributes, share, disposition,
                              options | sync_option | _FILE_OPEN_FOR_BACKUP_INTENT | _FILE_OPEN_REPARSE_POINT,
                              None, 0)
    if status < 0:
        raise NtError(status, f"open {_display(name)!r}")
    return handle.value


def _open_path(path: str, access: int, share: int) -> int:
    """A handle on `path` itself (long-path form, reparse points not followed). Raises OSError."""
    h = _k.CreateFileW(_long(path), access | _SYNCHRONIZE, share, None, 3, 0x02000000 | 0x00200000, None)
    if not h or h == _INVALID:
        err = ctypes.get_last_error()
        if err in (2, 3):
            raise FileNotFoundError(err, "not found", path)
        raise OSError(None, f"open {path}: {ctypes.FormatError(err).strip()}", None, err)
    return h


def _info(h: int) -> "paths._BHFI":
    info = paths._BHFI()
    if not _k.GetFileInformationByHandle(h, ctypes.byref(info)):
        raise _winerror("GetFileInformationByHandle")
    return info


def _file_id(info) -> tuple[int, int, int]:
    return info.dwVolumeSerialNumber, info.nFileIndexHigh, info.nFileIndexLow


def _is_plain_dir(info) -> bool:
    return bool(info.dwFileAttributes & _DIRECTORY) and not info.dwFileAttributes & _REPARSE


def _list_batch(h: int, buf) -> Optional[list[bytes]]:
    """The next batch of names in the directory `h` (continuing its enumeration cursor); None at the end."""
    if not _k.GetFileInformationByHandleEx(h, _FileFullDirectoryInfo, buf, len(buf)):
        if ctypes.get_last_error() == _ERROR_NO_MORE_FILES:
            return None
        raise _winerror("directory enumeration")
    out, offset, base = [], 0, ctypes.addressof(buf)
    while True:
        nxt = ctypes.c_ulong.from_address(base + offset).value
        length = ctypes.c_ulong.from_address(base + offset + 60).value
        name = ctypes.string_at(base + offset + 68, length)
        if name not in (b".\x00", b".\x00.\x00"):
            out.append(name)
        if not nxt:
            return out
        offset += nxt


def _set_dacl(h: int, sddl: str) -> None:
    psd = _vp()
    if not _a.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(psd), None):
        raise _winerror("ConvertStringSecurityDescriptorToSecurityDescriptorW")
    try:
        if not _a.SetKernelObjectSecurity(h, _DACL_AND_PROTECT, psd):
            raise _winerror("SetKernelObjectSecurity")
    finally:
        _k.LocalFree(psd)


def _owner_sid(h: int) -> str:
    size = ctypes.c_ulong()
    _a.GetKernelObjectSecurity(h, 1, None, 0, ctypes.byref(size))  # OWNER_SECURITY_INFORMATION
    buf = ctypes.create_string_buffer(max(size.value, 64))
    if not _a.GetKernelObjectSecurity(h, 1, buf, len(buf), ctypes.byref(size)):
        raise _winerror("GetKernelObjectSecurity")
    owner, defaulted = _vp(), ctypes.c_int()
    if not _a.GetSecurityDescriptorOwner(buf, ctypes.byref(owner), ctypes.byref(defaulted)) or not owner.value:
        raise _winerror("GetSecurityDescriptorOwner")
    text = ctypes.c_wchar_p()
    if not _a.ConvertSidToStringSidW(owner, ctypes.byref(text)):
        raise _winerror("ConvertSidToStringSidW")
    try:
        return text.value or ""
    finally:
        _k.LocalFree(ctypes.cast(text, _vp))


def _reset_dacl(parent: int, name: bytes, user: str) -> bool:
    """Recovery for an entry whose DACL a command changed (deny ACE): the orchestrator user owns every object
    in a run directory and resets the DACL through the owner's implicit WRITE_DAC, on a handle opened relative to
    the held parent without following links. False when that is not possible (e.g. an OWNER RIGHTS ACE removed
    the implicit right): the entry is then residue. Never on a name of a multi-link file: its DACL is shared
    with names elsewhere (e.g. a hardlink to a file outside the run directory)."""
    try:
        # FILE_READ_ATTRIBUTES is granted through the parent's FILE_LIST_DIRECTORY even under a deny-all DACL.
        h = _nt_open(parent, name, _READ_CONTROL | _WRITE_DAC | _FILE_READ_ATTRIBUTES, _SHARE_ALL, _FILE_OPEN, 0,
                     synchronous=False)
    except OSError:
        return False
    try:
        info = _info(h)
        if not info.dwFileAttributes & _DIRECTORY and info.nNumberOfLinks != 1:
            return False
        if _owner_sid(h) != user:
            return False
        _set_dacl(h, f"D:P(A;;FA;;;{user})")
    except OSError:
        return False
    finally:
        _close(h)
    return True


def _open_entry(parent: int, name: bytes, user: str, resets: Optional[list[int]] = None) -> tuple[int, object]:
    """(handle, info) of the entry `name` of the held directory `parent`, opened for deletion without
    following a link (a reparse point is opened as itself). `resets[0]` counts DACL resets (one summary line
    per removal is logged by the caller, not one per entry)."""
    for attempt in (0, 1):
        try:
            try:
                h = _nt_open(parent, name, _DELETE | _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES, _SHARE_ALL,
                             _FILE_OPEN, _FILE_DIRECTORY_FILE)
            except NtError as exc:
                if exc.status != _STATUS_NOT_A_DIRECTORY:
                    raise
                h = _nt_open(parent, name, _DELETE | _FILE_READ_ATTRIBUTES, _SHARE_ALL, _FILE_OPEN,
                             _FILE_NON_DIRECTORY_FILE)
        except NtError as exc:
            if exc.status == _STATUS_ACCESS_DENIED and attempt == 0 and _reset_dacl(parent, name, user):
                if resets is not None:
                    resets[0] += 1
                continue
            raise
        try:
            return h, _info(h)
        except BaseException:
            _close(h)
            raise
    raise OSError("unreachable")


def _disposition_ex(h: int) -> bool:
    flags = ctypes.c_ulong(_DISPOSITION_EX_FLAGS)
    return bool(_k.SetFileInformationByHandle(h, _FileDispositionInfoEx, ctypes.byref(flags), 4))


def _dispose(h: int, info, parent: Optional[int], name: Optional[bytes]) -> None:
    """Delete the entry open as `h` (removed from its directory when `h` is closed)."""
    if _disposition_ex(h):
        return
    first = ctypes.get_last_error()
    # Older file systems: clear the read-only attribute through a handle on the same object (never for a name
    # of a multi-link file, whose attributes are shared with names elsewhere), then a classic delete.
    if info.dwFileAttributes & _READONLY:
        if parent is None or name is None:
            raise OSError(None, "read-only entry cannot be deleted", None, first)
        if not info.dwFileAttributes & _DIRECTORY and info.nNumberOfLinks != 1:
            raise OSError(None, "read-only name of a multi-link file left in place", None, first)
        a = _nt_open(parent, name, _FILE_WRITE_ATTRIBUTES | _FILE_READ_ATTRIBUTES, _SHARE_ALL, _FILE_OPEN, 0)
        try:
            if _file_id(_info(a)) != _file_id(info):
                raise OSError("entry changed during removal")
            basic = _FileBasicInformation(0, 0, 0, 0, (info.dwFileAttributes & 0x3126) or _NORMAL)
            if not _k.SetFileInformationByHandle(a, _FileBasicInfo, ctypes.byref(basic), ctypes.sizeof(basic)):
                raise _winerror("clear read-only attribute")
        finally:
            _close(a)
    flag = ctypes.c_ubyte(1)
    if not _k.SetFileInformationByHandle(h, _FileDispositionInfo, ctypes.byref(flag), 1):
        raise _winerror(f"delete (extended disposition error {first})")


def remove_below(root: int, label: str, *, deadline: Optional[float] = None,
                 max_entries: Optional[int] = None) -> list[str]:
    """Delete everything inside the held directory `root` (not `root` itself) through handles opened relative
    to their held parent, without following links. Returns failure descriptions ([] = empty now)."""
    from orchestrator.security import acl

    user = acl.current_user_sid()
    limit = REMOVE_MAX_ENTRIES if max_entries is None else max_entries
    failures: list[str] = []
    buf = ctypes.create_string_buffer(64 * 1024)
    # frame: [handle, pending names, label, (parent handle, name, info) | None for root, enumeration finished]
    stack: list[list] = [[root, [], label, None, False]]
    seen = 0
    resets = [0]
    try:
        while stack:
            frame = stack[-1]
            if not frame[1] and not frame[4]:
                try:
                    batch = _list_batch(frame[0], buf)
                except OSError as exc:
                    failures.append(f"{frame[2]}: cannot list: {exc}")
                    batch = None
                if batch is None:
                    frame[4] = True
                else:
                    frame[1] = batch
                continue
            if frame[1]:
                name = frame[1].pop()
                seen += 1
                if seen > limit or (deadline is not None and time.monotonic() > deadline):
                    failures.append(f"{label}: removal bound reached ({'entries' if seen > limit else 'time'})")
                    return failures
                child = f"{frame[2]}\\{_display(name)}"
                try:
                    h, info = _open_entry(frame[0], name, user, resets)
                except OSError as exc:
                    failures.append(f"{child}: {exc}")
                    continue
                if _is_plain_dir(info):
                    if len(stack) >= REMOVE_MAX_DEPTH:
                        _close(h)
                        failures.append(f"{child}: nested deeper than {REMOVE_MAX_DEPTH}")
                        continue
                    stack.append([h, [], child, (frame[0], name, info), False])
                    continue
                try:
                    _dispose(h, info, frame[0], name)
                except OSError as exc:
                    failures.append(f"{child}: {exc}")
                finally:
                    _close(h)
                continue
            stack.pop()
            h, _pending, child, origin, _done = frame
            if origin is None:
                continue
            try:
                _dispose(h, origin[2], origin[0], origin[1])
            except OSError as exc:
                failures.append(f"{child}: {exc}")
            finally:
                _close(h)
    finally:
        for frame in stack[1:]:
            _close(frame[0])
        if resets[0]:
            log.warning("SECURITY: reset %d DACL(s) changed by a sandboxed verification command below %s",
                        resets[0], label)
    return failures


def remove_path(path: str | os.PathLike[str], *, deadline: Optional[float] = None,
                expected_id: Optional[tuple[int, int, int]] = None) -> None:
    """Remove `path` (a run directory, a legacy scratch dir, or a single entry) without following links.

    Raises ResidueRetained when something is left, OSError when the entry is not the expected object
    (replaced: tamper evidence) or is held open by a live owner (sharing violation; skipped by sweeps)."""
    p = os.path.abspath(os.fspath(path))
    if os.path.dirname(p) == p:
        raise paths.PathPolicyError("refusing to remove a filesystem root")
    try:
        h = _open_path(p, _DELETE | _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES, _SHARE_ALL)
    except FileNotFoundError:
        return  # already gone
    try:
        info = _info(h)
        if expected_id is not None and _file_id(info) != expected_id:
            raise OSError(f"{p} is no longer the directory the orchestrator created")
        failures = remove_below(h, p, deadline=deadline) if _is_plain_dir(info) else []
        if not failures:
            try:
                _dispose(h, info, None, None)
            except OSError as exc:
                failures.append(f"{p}: {exc}")
    finally:
        _close(h)
    if failures:
        raise ResidueRetained(None, f"{len(failures)} entries could not be removed from {p} "
                                    f"(first: {'; '.join(failures[:3])})")
    if os.path.lexists(p):
        raise ResidueRetained(None, f"{p} still exists after removal")


# ------------------------------------------------------------------------------------------ held-handle sweep

_HOLD_ACCESS = _FILE_LIST_DIRECTORY | _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES
_HOLD_SHARE = _SHARE_READ | _SHARE_WRITE  # no delete sharing: the held directory cannot be renamed or replaced
# Allow ACEs with a write right for these let a sandboxed (Low / AppContainer) process write a project root.
_SANDBOX_REACHABLE_PREFIXES = ("S-1-15-",)  # AppContainer packages and capabilities, incl. ALL (RESTRICTED) PACKAGES
_SANDBOX_REACHABLE_SIDS = frozenset({"S-1-1-0", "S-1-5-7"})  # Everyone, Anonymous


class _Busy(OSError):
    """The entry is held (or being deleted) by a live owner, possibly another orchestrator process."""


def _hold_dir_path(path: str, access: int = _HOLD_ACCESS) -> int:
    """A handle on the plain directory `path` (final component not followed), held without delete sharing."""
    h = _open_path(path, access, _HOLD_SHARE)
    try:
        if not _is_plain_dir(_info(h)):
            raise OSError(f"{path} is not a plain directory")
    except BaseException:
        _close(h)
        raise
    return h


def _hold_child_dir(parent: int, name: str, *, access: int = _HOLD_ACCESS, create: bool = False) -> int:
    """The plain directory `name` of the held `parent`, opened relative to it and held without delete sharing."""
    h = _nt_open(parent, _encode(name), access, _HOLD_SHARE, _FILE_OPEN_IF if create else _FILE_OPEN,
                 _FILE_DIRECTORY_FILE, case_insensitive=True)
    try:
        if not _is_plain_dir(_info(h)):
            raise OSError(f"{name!r} is not a plain directory")
    except BaseException:
        _close(h)
        raise
    return h


def _list_names(h: int, bound: int = LIST_MAX_NAMES) -> list[bytes]:
    buf = ctypes.create_string_buffer(64 * 1024)
    names: list[bytes] = []
    while len(names) < bound:
        batch = _list_batch(h, buf)
        if batch is None:
            break
        names += batch
    return names[:bound]


def _untrusted_root(h: int) -> Optional[str]:
    """Why the held project root must not be swept (a Low label or a sandbox-reachable write grant), or None."""
    from orchestrator.security import python_runtime

    label, sec = python_runtime._read_handle_security(h)
    if label is not None and label[0] < sandbox.MEDIUM_RID:
        return "Low integrity label"
    if sec["aces"] is None:
        return "NULL DACL"
    for ace in sec["aces"]:
        sid = ace.get("sid") or ""
        if ace.get("type") == 0 and ace.get("mask", 0) & _WRITE_RIGHTS and (
                sid.startswith(_SANDBOX_REACHABLE_PREFIXES) or sid in _SANDBOX_REACHABLE_SIDS):
            return f"write grant for {sid}"
    return None


def _protect_handle(h: int) -> None:
    """Protected owner-only DACL (user, SYSTEM, Administrators; inheritable) on a directory the orchestrator
    created and holds (READ_CONTROL | WRITE_DAC)."""
    from orchestrator.security import acl

    _set_dacl(h, f"D:P(A;OICI;FA;;;{acl.current_user_sid()})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)")


def _rename_into(h: int, target_dir: int, name: bytes) -> None:
    """Rename the object open as `h` (DELETE access) to `name` inside the held directory `target_dir`, by handle
    (``FileRenameInformation`` with ``RootDirectory``): no path is resolved. Never replaces. Raises NtError."""
    offset = _FileRenameHeader.FileNameLength.offset + ctypes.sizeof(ctypes.c_ulong)
    buf = ctypes.create_string_buffer(offset + len(name) + 2)
    header = _FileRenameHeader.from_buffer(buf)
    header.ReplaceIfExists = 0
    header.RootDirectory = target_dir
    header.FileNameLength = len(name)
    ctypes.memmove(ctypes.addressof(buf) + offset, name, len(name))
    iosb = _IoStatusBlock()
    status = _nt.NtSetInformationFile(h, ctypes.byref(iosb), buf, len(buf), _FileRenameInformation)
    if status < 0:
        raise NtError(status, f"rename into the residue area as {_display(name)!r}")


def _backoff_due(fid: tuple[int, int, int], now: float) -> bool:
    with _LIVE_LOCK:
        entry = _BACKOFF.get(fid)
    return entry is None or now >= entry[1]


def _backoff_record(fid: tuple[int, int, int], now: float) -> None:
    with _LIVE_LOCK:
        attempts = _BACKOFF.get(fid, (0, 0.0))[0] + 1
        _BACKOFF[fid] = (attempts, now + min(RESIDUE_BACKOFF_MAX, RESIDUE_BACKOFF_FIRST * 2 ** min(attempts - 1, 16)))
        if len(_BACKOFF) > 10_000:  # bounded: forget the oldest records (they are simply retried earlier)
            for key in list(_BACKOFF)[:1_000]:
                del _BACKOFF[key]


def _backoff_clear(fid: tuple[int, int, int]) -> None:
    with _LIVE_LOCK:
        _BACKOFF.pop(fid, None)


_DEFERRED = "retry deferred (backoff)"


def _remove_child(parent: int, name: bytes, label: str, *, deadline: Optional[float],
                  expected_id: Optional[tuple[int, int, int]] = None, backoff: bool = False,
                  quarantine: Optional[Callable[[], int]] = None) -> Optional[str]:
    """Remove the entry `name` of the held directory `parent`, opened relative to it without following links and
    held (no delete sharing) until it is gone or quarantined. None = removed (or already gone); otherwise the
    residue description. With `quarantine` (returns the held residue/ handle), a directory left with residue is
    renamed into it by handle. With `backoff`, an object whose earlier attempts failed is skipped until due.
    Raises _Busy (held or being deleted elsewhere) and OSError when the entry is not `expected_id` (tamper)."""
    try:
        h = _nt_open(parent, name, _DELETE | _HOLD_ACCESS, _HOLD_SHARE, _FILE_OPEN, 0)
    except NtError as exc:
        if exc.status in (_STATUS_OBJECT_NAME_NOT_FOUND, _STATUS_OBJECT_PATH_NOT_FOUND):
            return None
        if exc.status in (_STATUS_SHARING_VIOLATION, _STATUS_DELETE_PENDING):
            raise _Busy(None, f"{label}: in use ({exc})", None, _ERROR_SHARING_VIOLATION) from None
        return f"{label}: {exc}"
    try:
        info = _info(h)
        fid = _file_id(info)
        if expected_id is not None and fid != expected_id:
            raise OSError(f"{label} is no longer the directory the orchestrator created")
        now = time.monotonic()
        if backoff and not _backoff_due(fid, now):
            return f"{label}: {_DEFERRED}"
        failures = remove_below(h, label, deadline=deadline) if _is_plain_dir(info) else []
        if not failures:
            try:
                _dispose(h, info, parent, name)
                _backoff_clear(fid)
                return None
            except OSError as exc:
                failures.append(f"{label}: {exc}")
        if backoff:
            _backoff_record(fid, now)
        detail = f"{len(failures)} entries could not be removed from {label} (first: {'; '.join(failures[:3])})"
        if quarantine is not None and _is_plain_dir(info):
            try:
                target = quarantine()
                try:
                    _rename_into(h, target, name)
                except NtError as exc:
                    if exc.status != _STATUS_OBJECT_NAME_COLLISION:
                        raise
                    _rename_into(h, target, name + _encode("-" + os.urandom(4).hex()))
                detail += f"; moved to {RESIDUE_DIR}/"
            except OSError as exc:
                detail += f"; not moved to {RESIDUE_DIR}/: {exc}"
        return detail
    finally:
        _close(h)


class _RootSweep:
    """One bounded sweep of a held project root (see `sweep`)."""

    def __init__(self, root_h: int, label: str, deadline: float, limit: int):
        self.root_h, self.label, self.deadline, self.limit = root_h, label, deadline, limit
        self.residue_h: Optional[int] = None
        self.retained: list[str] = []

    def residue_dir(self) -> int:
        if self.residue_h is None:
            self.residue_h = _hold_residue_dir(self.root_h)
        return self.residue_h

    def close(self) -> None:
        _close(self.residue_h)
        self.residue_h = None

    def _remove(self, parent: int, name: bytes, *, quarantine: bool) -> bool:
        """False once the time budget is spent."""
        if time.monotonic() > self.deadline:
            self.retained.append(f"{self.label}: sweep time budget reached")
            return False
        child = f"{self.label}\\{_display(name)}"
        try:
            left = _remove_child(parent, name, child, deadline=self.deadline, backoff=True,
                                 quarantine=self.residue_dir if quarantine else None)
        except _Busy:
            return True  # held by a live session (possibly of another process)
        except OSError as exc:
            left = str(exc)
        if left is not None and not left.endswith(_DEFERRED):
            self.retained.append(left)
        return True

    def run(self) -> list[str]:
        why = _untrusted_root(self.root_h)
        if why:
            key = _key(self.label)
            with _LIVE_LOCK:
                first = key not in _UNTRUSTED_WARNED
                _UNTRUSTED_WARNED.add(key)
            if first:
                log.warning("SECURITY: verification sandbox root %s not swept (%s); it is rebuilt before its next "
                            "use", self.label, why)
            return [f"{self.label}: not swept ({why})"]
        names = _list_names(self.root_h)
        decoded = {n.decode("utf-16-le", "replace").casefold(): n for n in names}
        budget = self.limit
        # Legacy per-project layout of the version before copy-in.
        for text, name in decoded.items():
            if budget <= 0:
                break
            if text == "tmp" or text.startswith("cache-"):
                budget -= 1
                if not self._remove(self.root_h, name, quarantine=False):
                    return self.retained
        if "runs" in decoded and budget > 0:
            try:
                runs_h = _hold_child_dir(self.root_h, "runs")
            except OSError as exc:
                self.retained.append(f"{self.label}\\runs: {exc}")
            else:
                try:
                    for name in _list_names(runs_h):
                        if budget <= 0:
                            break
                        if is_live(os.path.join(self.label, "runs", _display(name))):
                            continue
                        budget -= 1
                        if not self._remove(runs_h, name, quarantine=True):
                            return self.retained
                finally:
                    _close(runs_h)
        if RESIDUE_DIR in decoded:
            self._sweep_residue(budget)
        return self.retained

    def _sweep_residue(self, budget: int) -> None:
        """At most every RESIDUE_SCAN_SECONDS per root and process; each object additionally backs off."""
        key, now = _key(self.label), time.monotonic()
        with _LIVE_LOCK:
            if now < _RESIDUE_NEXT_SCAN.get(key, 0.0):
                return
            _RESIDUE_NEXT_SCAN[key] = now + RESIDUE_SCAN_SECONDS
        try:
            residue_h = self.residue_dir()
        except OSError as exc:
            self.retained.append(f"{self.label}\\{RESIDUE_DIR}: {exc}")
            return
        for name in _list_names(residue_h):
            if budget <= 0:
                break
            budget -= 1
            if not self._remove(residue_h, name, quarantine=False):
                return


def _hold_residue_dir(root_h: int) -> int:
    """`residue/` of the held project root (created when missing), held, with a protected owner-only DACL."""
    h = _hold_child_dir(root_h, RESIDUE_DIR, access=_HOLD_ACCESS | _READ_CONTROL | _WRITE_DAC, create=True)
    try:
        _protect_handle(h)
    except BaseException:
        _close(h)
        raise
    return h


def _sweep_held(root_h: int, label: str, deadline: float, limit: int) -> list[str]:
    job = _RootSweep(root_h, label, deadline, limit)
    try:
        return job.run()
    except OSError as exc:
        log.warning("cannot inspect the verification sandbox %s for cleanup: %s", label, exc)
        return job.retained
    finally:
        job.close()


def sweep(sandbox_root: str | os.PathLike[str], *, budget: float = SWEEP_SECONDS,
          limit: int = SWEEP_MAX_ENTRIES) -> list[str]:
    """Bounded removal of what no live Session owns below one project sandbox root: `runs/<id>` directories
    left behind (residue, crashes), the legacy per-project `tmp/` and `cache-*` directories of the layout
    before copy-in and, at most every RESIDUE_SCAN_SECONDS with a per-object backoff, `residue/`. The root,
    `runs/` and `residue/` are held without delete sharing and every entry is opened relative to its held parent:
    nothing is removed by path. A root carrying a Low label or a sandbox-reachable write grant is not swept.
    Never follows links; never raises; returns what is still retained."""
    if os.name != "nt":
        return []
    root = os.path.abspath(os.fspath(sandbox_root))
    deadline = time.monotonic() + budget
    try:
        root_h = _hold_dir_path(root, _HOLD_ACCESS | _READ_CONTROL)
    except FileNotFoundError:
        return []
    except OSError as exc:
        log.warning("cannot inspect the verification sandbox %s for cleanup: %s", root, exc)
        return []
    try:
        retained = _sweep_held(root_h, root, deadline, limit)
    finally:
        _close(root_h)
    if retained:
        log.warning("verification sandbox residue retained for a later sweep: %s", "; ".join(retained[:5]))
    return retained


def sweep_all(sandbox_dir: str | os.PathLike[str], *, budget: float = SWEEP_SECONDS) -> list[str]:
    """`sweep` over every project below the sandbox dir (`<worktrees>/_sandbox`), within one time budget. The
    sandbox dir is held and every project root is opened relative to it."""
    if os.name != "nt":
        return []
    root = os.path.abspath(os.fspath(sandbox_dir))
    deadline = time.monotonic() + budget
    retained: list[str] = []
    try:
        dir_h = _hold_dir_path(root)
    except FileNotFoundError:
        return []
    except OSError as exc:
        log.warning("cannot inspect the verification sandbox dir %s: %s", root, exc)
        return []
    try:
        for name in _list_names(dir_h):
            if time.monotonic() > deadline:
                break
            label = os.path.join(root, _display(name))
            try:
                project_h = _hold_child_dir(dir_h, name.decode("utf-16-le", "surrogatepass"),
                                            access=_HOLD_ACCESS | _READ_CONTROL)
            except OSError:
                continue  # not a plain directory (file, junction) or gone: nothing of ours to sweep
            try:
                retained += _sweep_held(project_h, label, deadline, SWEEP_MAX_ENTRIES)
            finally:
                _close(project_h)
    except OSError as exc:
        log.warning("cannot inspect the verification sandbox dir %s: %s", root, exc)
    finally:
        _close(dir_h)
    if retained:
        log.warning("verification sandbox residue retained for a later sweep: %s", "; ".join(retained[:5]))
    return retained


def sweep_all_once(sandbox_dir: str | os.PathLike[str]) -> list[str]:
    """`sweep_all` the first time a sandbox dir is used in this process (startup cleanup of crash residue)."""
    key = _key(sandbox_dir)
    with _LIVE_LOCK:
        if key in _SWEPT_ONCE:
            return []
        _SWEPT_ONCE.add(key)
    return sweep_all(sandbox_dir)


def preflight(root: str | os.PathLike[str], *, check: Optional[Callable[[], None]] = None,
              max_entries: Optional[int] = None) -> None:
    """Refuse (SandboxTampered) a tree holding a reparse point or a multi-link file, before any copy or after
    it. Bounded; calls `check` (deadline / cancellation) while scanning."""
    limit = MAX_ENTRIES if max_entries is None else max_entries
    stack = [os.fspath(root)]
    seen = 0
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    seen += 1
                    if seen > limit:
                        raise sandbox.SandboxUnavailable(f"the worktree has more than {limit} entries; "
                                                         "verification tree not built")
                    if check is not None and seen % 64 == 0:
                        check()
                    st = os.stat(entry.path, follow_symlinks=False)  # a fresh no-follow stat (link count)
                    if sandbox.is_reparse(st) or (stat_mod.S_ISREG(st.st_mode) and st.st_nlink > 1):
                        raise sandbox.SandboxTampered("unsafe worktree or Git metadata before sandbox preparation")
                    if stat_mod.S_ISDIR(st.st_mode):
                        stack.append(entry.path)
        except OSError:
            raise sandbox.SandboxTampered("unsafe worktree or Git metadata before sandbox preparation") from None
    if check is not None:
        check()


class VerificationTree:
    def __init__(self, source: str | os.PathLike[str], sandbox_root: str | os.PathLike[str], *,
                 check: Optional[Callable[[], None]] = None):
        self.source = os.path.abspath(os.fspath(source))
        self.sandbox_root = Path(sandbox_root)
        self.base = self.sandbox_root / "runs" / os.urandom(8).hex()
        self.path = self.base / "tree"
        self._check = check
        self._created = False
        self._removed = False
        self._base_h: Optional[int] = None
        self._base_id: Optional[tuple[int, int, int]] = None
        # The project root and runs/ are held (no delete sharing) for the session: the run directory is created,
        # removed and quarantined relative to them, never by path.
        self._root_h: Optional[int] = None
        self._runs_h: Optional[int] = None
        self._residue_h: Optional[int] = None
        self.residue: Optional[str] = None
        self.entries = 0
        self.bytes = 0

    # ------------------------------------------------------------------------------------------- create

    def create(self) -> Path:
        """Build the copy. Raises SandboxTampered (unsafe source), SandboxUnavailable (anything else)."""
        from orchestrator.security import acl, appcontainer

        if self._created or self._removed:
            raise sandbox.SandboxUnavailable("verification trees are single-use")
        if os.name != "nt":
            raise sandbox.SandboxUnavailable("no sandbox implementation on this platform (Windows only)")
        # Same refusal as before the copy-in design: an alias already present in the worktree blocks the task.
        try:
            preflight(self.source, check=self._check)
            paths.read_inside(self.source, ".git", max_bytes=_GITLINK_BYTES)
        except (paths.PathPolicyError, OSError):
            raise sandbox.SandboxTampered("unsafe worktree or Git metadata before sandbox preparation") from None
        self._tick(count=False)
        with _LIVE_LOCK:
            _LIVE.add(_key(self.base))
        try:
            sandbox.medium_plain_root(self.sandbox_root)
            runs = sandbox.real_dir(self.sandbox_root / "runs", create=True)
            if sandbox._low_label_ok(str(runs)):
                raise sandbox.SandboxUnavailable(f"{runs} is writable at Low integrity; refusing to use it")
            _protect_runs(runs)
            self._root_h = _hold_dir_path(str(self.sandbox_root))
            self._runs_h = _hold_child_dir(self._root_h, "runs")
            # Fresh random name, created exclusively relative to the held runs/ (an existing entry is refused,
            # never adopted). Held (no delete sharing) for the whole session: not renamed, skipped by every sweep.
            self._base_h = self._create_dir(self._runs_h, self.base.name)
            self._created = True
            self._base_id = _file_id(_info(self._base_h))
            _refuse_package_writable(self.base)
            tree_h = self._create_dir(self._base_h, "tree")
            try:
                sid = appcontainer.task_sid(self.path)
                if any(ace["sid"] == sid for ace in acl.read_security(self.source)["aces"] or []):
                    raise sandbox.SandboxUnavailable("the task worktree carries a grant for this run's identity")
                # Empty directory, identity not yet instantiated: everything copied below inherits these.
                appcontainer.grant_directory(self.path, sid, writable=True)
                sandbox.label_low(self.path)
                sandbox.pin_directory(self.path)
                _refuse_package_writable(self.path)
                with paths.directory_guard(self.source):
                    self._copy_dir(self.source, tree_h, 0)
            finally:
                _close(tree_h)
        except BaseException as exc:
            try:
                self.destroy()
            except Exception:  # noqa: BLE001, S110 - the original failure is reported; sweeps retry residue
                pass
            if isinstance(exc, (sandbox.SandboxTampered, sandbox.SandboxUnavailable)) or not isinstance(
                    exc, (OSError, paths.PathPolicyError, appcontainer.AppContainerUnavailable)):
                raise
            raise sandbox.SandboxUnavailable(f"cannot build the verification tree: {exc}") from exc
        return self.path

    def _tick(self, *, count: bool = True) -> None:
        if count:
            self.entries += 1
            if self.entries > MAX_ENTRIES:
                raise sandbox.SandboxUnavailable(f"the worktree has more than {MAX_ENTRIES} entries; "
                                                 "verification tree not built")
        if self._check is not None:
            self._check()

    @staticmethod
    def _create_dir(parent: int, name: str) -> int:
        """A new directory relative to the held `parent`, returned held (no delete sharing)."""
        try:
            h = _nt_open(parent, _encode(name), _FILE_LIST_DIRECTORY | _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES,
                         _SHARE_READ | _SHARE_WRITE, _FILE_CREATE, _FILE_DIRECTORY_FILE, case_insensitive=True)
        except NtError as exc:
            if exc.status == _STATUS_OBJECT_NAME_COLLISION:
                raise sandbox.SandboxUnavailable(f"name collision in the verification copy: {name!r}") from None
            raise
        try:
            if not _is_plain_dir(_info(h)):
                raise sandbox.SandboxUnavailable("a created copy directory is not a plain directory")
        except BaseException:
            _close(h)
            raise
        return h

    def _hold_source_dir(self, src: str) -> int:
        """A handle on `src` that forbids its rename/removal while held; refused unless it is a plain directory
        whose final path is inside the source worktree."""
        h = paths._k32.CreateFileW(src, paths._GENERIC_READ, 0x1, None, paths._OPEN_EXISTING,
                                   paths._FLAG_BACKUP | paths._FLAG_OPEN_REPARSE, None)
        if not h or h == paths._INVALID:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            info = paths._BHFI()
            if not paths._k32.GetFileInformationByHandle(h, ctypes.byref(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            if info.dwFileAttributes & paths._REPARSE_ATTR or not info.dwFileAttributes & paths._ATTR_DIRECTORY:
                raise sandbox.SandboxTampered("the worktree changed while it was copied (not a plain directory)")
            if not paths._contained(self.source, paths._final_path(h)):
                raise sandbox.SandboxTampered("the worktree changed while it was copied (directory escaped)")
        except BaseException:
            paths._k32.CloseHandle(h)
            raise
        return h

    def _copy_dir(self, src: str, dst: int, depth: int) -> None:
        if depth > MAX_DEPTH:
            raise sandbox.SandboxUnavailable(f"the worktree is nested deeper than {MAX_DEPTH} directories")
        held = self._hold_source_dir(src)
        try:
            with os.scandir(src) as entries:
                names = sorted(entry.name for entry in entries)
            for name in names:
                self._tick()
                s = os.path.join(src, name)
                st = os.lstat(s)
                if sandbox.is_reparse(st):
                    raise sandbox.SandboxTampered("the worktree changed while it was copied (link appeared)")
                if stat_mod.S_ISDIR(st.st_mode):
                    d = self._create_dir(dst, name)
                    try:
                        self._copy_dir(s, d, depth + 1)
                    finally:
                        _close(d)
                elif stat_mod.S_ISREG(st.st_mode):
                    self._copy_file(s, dst, name)
                else:
                    raise sandbox.SandboxTampered("the worktree holds an entry that is neither file nor directory")
        finally:
            paths._k32.CloseHandle(held)

    def _copy_file(self, src: str, dst_dir: int, name: str) -> None:
        try:
            fd, _size = paths._open_fd(src, self.source, write=False)  # no-follow; link count and containment
        except IsADirectoryError:
            raise sandbox.SandboxTampered("the worktree changed while it was copied (file became a directory)") from None
        except paths.PathPolicyError as exc:
            raise sandbox.SandboxTampered(f"the worktree changed while it was copied: {exc}") from None
        with os.fdopen(fd, "rb") as reader:
            source_stat = os.fstat(reader.fileno())
            try:
                out = _nt_open(dst_dir, _encode(name), _GENERIC_WRITE, 0, _FILE_CREATE, _FILE_NON_DIRECTORY_FILE,
                               attributes=_NORMAL, case_insensitive=True)
            except NtError as exc:
                if exc.status == _STATUS_OBJECT_NAME_COLLISION:
                    raise sandbox.SandboxUnavailable(f"name collision in the verification copy: {name!r}") from None
                raise
            try:
                while True:
                    self._tick(count=False)  # deadline / cancellation inside large files too
                    chunk = reader.read(_CHUNK)
                    if not chunk:
                        break
                    self.bytes += len(chunk)
                    if self.bytes > MAX_BYTES:
                        raise sandbox.SandboxUnavailable(f"the worktree holds more than {MAX_BYTES} bytes; "
                                                         "verification tree not built")
                    view, done = memoryview(chunk), 0
                    while done < len(chunk):
                        written = ctypes.c_ulong()
                        piece = view[done:]
                        cbuf = (ctypes.c_char * len(piece)).from_buffer_copy(piece)
                        if not _k.WriteFile(out, cbuf, len(piece), ctypes.byref(written), None) or not written.value:
                            raise _winerror("WriteFile")
                        done += written.value
                # Timestamps only (stale-bytecode and incremental-build checks), on the created file's handle.
                atime = ctypes.c_ulonglong(source_stat.st_atime_ns // 100 + _EPOCH_AS_FILETIME)
                mtime = ctypes.c_ulonglong(source_stat.st_mtime_ns // 100 + _EPOCH_AS_FILETIME)
                if not _k.SetFileTime(out, None, ctypes.byref(atime), ctypes.byref(mtime)):
                    raise _winerror("SetFileTime")
            finally:
                _close(out)

    # ------------------------------------------------------------------------------------------ destroy

    @property
    def exists(self) -> bool:
        return self._created and not self._removed

    def _release(self) -> None:
        for name in ("_base_h", "_runs_h", "_root_h"):
            _close(getattr(self, name))
            setattr(self, name, None)
        with _LIVE_LOCK:
            _LIVE.discard(_key(self.base))

    def destroy(self, *, deadline: Optional[float] = None) -> None:
        """Remove base (copy, scratch, caches) without following links, within `deadline` (default
        DESTROY_SECONDS from now). Call only after every process of the command has ended. Raises ResidueRetained
        (contained; the run directory is moved into `residue/`, logged, retried by `sweep`) or OSError (the run
        directory is not the one created: tamper evidence)."""
        if not self._created or self._removed:
            if not self._created:
                self._release()
            return
        if deadline is None:
            deadline = time.monotonic() + DESTROY_SECONDS
        label = str(self.base)
        try:
            if self._runs_h is None or self._base_h is None:  # pragma: no cover - created implies both are held
                raise OSError("verification run directory handles are not held")
            # The bulk of the removal happens while the run directory is still held: no other deleter (another
            # orchestrator process's sweep) can open it meanwhile.
            remove_below(self._base_h, label, deadline=deadline)
            # Removing the directory itself needs DELETE, which the held handle does not carry (its share mode would
            # break the ancestor guards used during the session). Reopened relative to the held runs/ and checked by
            # file id; a concurrent removal by another orchestrator process in between is residue/retry, not tamper.
            _close(self._base_h)
            self._base_h = None
            root_h = self._root_h
            try:
                left = _remove_child(self._runs_h, _encode(self.base.name), label, deadline=deadline,
                                     expected_id=self._base_id, quarantine=lambda: self._residue_dir(root_h))
            except _Busy as exc:
                left = f"{exc}; retried by a later sweep"
            if left is not None:
                exc = ResidueRetained(None, left)
                self.residue = left
                log.warning("SECURITY: verification run directory retained for a later sweep (contained): %s", left)
                raise exc
        finally:
            self._close_residue()
            self._release()
        self._removed = True

    def _residue_dir(self, root_h: Optional[int]) -> int:
        if root_h is None:
            raise OSError("the project sandbox root is not held")
        if self._residue_h is None:
            self._residue_h = _hold_residue_dir(root_h)
        return self._residue_h

    def _close_residue(self) -> None:
        _close(self._residue_h)
        self._residue_h = None


__all__ = ["MAX_BYTES", "MAX_DEPTH", "MAX_ENTRIES", "PACKAGE_WIDE_SIDS", "RESIDUE_DIR", "NtError",
           "ResidueRetained", "VerificationTree", "is_live", "package_write_aces", "preflight", "remove_below",
           "remove_path", "sweep", "sweep_all", "sweep_all_once"]
