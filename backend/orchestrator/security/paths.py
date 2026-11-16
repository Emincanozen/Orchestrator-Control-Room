"""Path policy (ARCHITECTURE §12).

Agent cwd and every file-serving endpoint must resolve inside a registered
project path or its worktree. Rejects: NUL bytes, UNC / device paths, NTFS
alternate data streams, traversal and symlink/junction escapes (checked after
full resolution).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from contextlib import ExitStack, contextmanager
from pathlib import Path, PureWindowsPath

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class PathPolicyError(Exception):
    """A path violates the path policy."""


def _check_raw(raw: str) -> None:
    if not raw or "\x00" in raw:
        raise PathPolicyError("empty path or NUL byte")
    norm = raw.replace("/", "\\")
    if norm.startswith("\\\\"):
        raise PathPolicyError("UNC and device paths are not allowed")
    # ':' is only legal as the drive separator (C:). Anything else is an ADS or device spec.
    rest = norm[2:] if _DRIVE_RE.match(norm) else norm
    if ":" in rest:
        raise PathPolicyError("alternate data streams / device specifiers are not allowed")


def validate_root(raw: str | os.PathLike[str], *, protected: Iterable[str | os.PathLike[str]] = ()) -> Path:
    """Validate a path that is about to become a root (project registration, security finding #7).

    Refused after full resolution (realpath: junctions, symlinks and 8.3 short names are resolved first):
    drive roots; the user profile, its parents, %APPDATA%, %LOCALAPPDATA%, %ProgramData% themselves (and any
    directory containing them); %SystemRoot%, %ProgramFiles% and %ProgramFiles(x86)% themselves and everything
    inside them; and any root that is, contains or is inside one of `protected` (data dir, worktrees dir) or of
    the orchestrator's own installation (checkout, config dir, venv/interpreter, dashboard build; re-review N3),
    which is protected whatever the caller passes."""
    raw_s = os.fspath(raw)
    _check_raw(raw_s)
    if os.name == "nt" and PureWindowsPath(raw_s).drive and not PureWindowsPath(raw_s).is_absolute():
        raise PathPolicyError("drive-relative paths are not allowed")
    p = Path(raw_s)
    if not p.is_absolute():
        raise PathPolicyError("project path must be absolute")
    resolved = Path(os.path.realpath(p))
    _check_raw(str(resolved))
    if not resolved.is_dir():
        raise PathPolicyError("project path is not an existing directory")
    _check_sensitive(resolved, protected)
    return resolved


def _env_dirs(*names: str) -> list[Path]:
    out = []
    for n in names:
        v = os.environ.get(n)
        if v and os.path.isabs(v):
            out.append(Path(v))
    return out


def sensitive_dirs() -> tuple[list[Path], list[Path]]:
    """(exact, trees): `exact` may not be a project root nor be contained in one; `trees` additionally may not
    contain a project root. Both are resolved with realpath."""
    profiles = [Path.home(), *_env_dirs("USERPROFILE")]
    exact: list[Path] = [*profiles, *(p.parent for p in profiles)]  # the profile and C:\Users (/home)
    exact += _env_dirs("APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "ALLUSERSPROFILE", "PUBLIC")
    trees = _env_dirs("SYSTEMROOT", "WINDIR", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432")
    if os.name == "nt":
        drive = os.environ.get("SYSTEMDRIVE", "C:")
        trees += [Path(drive + "\\Windows"), Path(drive + "\\Program Files"), Path(drive + "\\Program Files (x86)")]
        exact += [Path(drive + "\\ProgramData")]
    else:
        trees += [Path(x) for x in ("/bin", "/sbin", "/usr", "/etc", "/lib", "/lib64", "/boot", "/proc", "/sys", "/dev")]

    def real(ps: list[Path]) -> list[Path]:
        return list({_norm(Path(os.path.realpath(x))): Path(os.path.realpath(x)) for x in ps}.values())

    return real(exact), real(trees)


def _is_drive_root(p: Path) -> bool:
    return p.parent == p or _norm(p) == _norm(Path(p.anchor))


def _check_sensitive(resolved: Path, protected: Iterable[str | os.PathLike[str]]) -> None:
    if _is_drive_root(resolved):
        raise PathPolicyError("a drive root cannot be a project")
    exact, trees = sensitive_dirs()
    for d in exact:
        if is_within(d, resolved):
            raise PathPolicyError(f"{resolved} is or contains a system/user profile directory ({d}); register a "
                                  "project folder instead")
    for d in trees:
        if is_within(d, resolved) or is_within(resolved, d):
            raise PathPolicyError(f"{resolved} is inside or contains a system directory ({d})")
    for raw in protected:
        d = Path(os.path.realpath(os.fspath(raw)))
        if is_within(d, resolved) or is_within(resolved, d):
            raise PathPolicyError("a project cannot be, contain or be inside the orchestrator data or worktrees "
                                  f"directory ({d})")
    from orchestrator.config import install_dirs

    for d in install_dirs():
        if is_within(d, resolved) or is_within(resolved, d):
            raise PathPolicyError(f"a project cannot be, contain or be inside the orchestrator's own installation "
                                  f"({d}: its code, configuration or Python environment)")


def _norm(p: Path) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


def is_within(path: Path, root: Path) -> bool:
    p, r = _norm(path), _norm(root)
    if p == r:
        return True
    return p.startswith(r.rstrip("\\/") + os.sep)


def resolve_inside(raw: str | os.PathLike[str], roots: Iterable[str | os.PathLike[str]], *, base: str | None = None) -> Path:
    """Resolve `raw` (absolute, or relative to `base`) and require it to stay inside one of `roots`.

    Symlinks and junctions are followed by realpath, so an escape via a link is
    detected on the final target. Returns the resolved path.
    """
    raw_s = os.fspath(raw)
    _check_raw(raw_s)
    if os.name == "nt" and PureWindowsPath(raw_s).drive and not PureWindowsPath(raw_s).is_absolute():
        raise PathPolicyError("drive-relative paths are not allowed")
    p = Path(raw_s)
    if not p.is_absolute():
        if base is None:
            raise PathPolicyError("relative path without a base")
        p = Path(base) / p
    resolved = Path(os.path.realpath(p))
    _check_raw(str(resolved))
    real_roots = [Path(os.path.realpath(os.fspath(r))) for r in roots]
    for root in real_roots:
        if is_within(resolved, root):
            return resolved
    raise PathPolicyError("path is outside every registered project and worktree")


# --------------------------------------------------------------------------------------------------------------
# No-follow file access inside a directory that another (sandboxed or agent) process can write (N2 re-review S11).
#
# `resolve_inside` + a later `open` is a check-then-use race: a concurrent writer can swap a checked file or one of
# its parent directories for a junction / symlink after the check, and the Medium server then reads (or writes)
# outside the worktree. The helpers below never follow a reparse point:
#   * every intermediate component is lstat-ed and must be a plain directory;
#   * the final component is opened with FILE_FLAG_OPEN_REPARSE_POINT (the link itself, never its target) and
#     refused when it is a reparse point, not a regular file, or a hardlink (link count > 1: a hardlink can make a
#     file outside the tree appear inside it);
#   * containment is re-checked on the OPENED HANDLE (GetFinalPathNameByHandleW), which catches a parent swapped
#     between the lstat walk and the open.
# --------------------------------------------------------------------------------------------------------------


class FileTooLarge(PathPolicyError):
    def __init__(self, size: int, limit: int):
        super().__init__(f"file too large ({size} bytes > {limit})")
        self.size = size
        self.limit = limit


_REPARSE_ATTR = 0x400


def _rel_parts(rel: str | os.PathLike[str]) -> list[str]:
    raw = os.fspath(rel)
    _check_raw(raw)
    norm = raw.replace("\\", "/")
    if norm.startswith("/") or _DRIVE_RE.match(norm):
        raise PathPolicyError("an absolute path is not allowed here")
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise PathPolicyError("path must be relative and stay inside the directory")
    return parts


def _is_link(st: os.stat_result) -> bool:
    import stat as stat_mod

    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE_ATTR) or stat_mod.S_ISLNK(st.st_mode)


def _walk_parents(root_real: str, parts: list[str], *, create: bool = False) -> str:
    """Join `parts[:-1]` onto `root_real`, requiring each to be a plain directory (created when `create`)."""
    import stat as stat_mod

    cur = root_real
    for name in parts[:-1]:
        cur = os.path.join(cur, name)
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            if not create:
                raise
            os.mkdir(cur)
            st = os.lstat(cur)
        if _is_link(st):
            raise PathPolicyError(f"a path component is a junction/symlink: {name}")
        if not stat_mod.S_ISDIR(st.st_mode):
            raise PathPolicyError(f"a path component is not a directory: {name}")
    return os.path.join(cur, parts[-1])


@contextmanager
def directory_guard(directory: str | os.PathLike[str], *, create: bool = False):
    """Pin every ancestor of an absolute plain directory until the operation finishes.

    Windows handles deny WRITE and DELETE sharing, preventing rename/replacement of the
    entire chain. OPEN_REPARSE_POINT inspects the entry itself, never its target.
    No permissions or integrity labels are changed. Other platforms only validate.
    """
    import stat as stat_mod

    raw = os.fspath(directory)
    _check_raw(raw)
    absolute = Path(os.path.abspath(raw))
    if ".." in Path(raw).parts:
        raise PathPolicyError("parent traversal is not allowed")
    with ExitStack() as held:
        cur = Path(absolute.anchor)
        for name in ("", *absolute.parts[1:]):
            if name:
                cur /= name
            if create and not os.path.lexists(cur):
                try:
                    os.mkdir(cur)
                except FileExistsError:
                    pass
            if os.name == "nt":
                h = _k32.CreateFileW(str(cur), _GENERIC_READ, 0x1, None, _OPEN_EXISTING,
                                     _FLAG_BACKUP | _FLAG_OPEN_REPARSE, None)
                if not h or h == _INVALID:
                    raise ctypes.WinError(ctypes.get_last_error())
                held.callback(_k32.CloseHandle, h)
                info = _BHFI()
                if not _k32.GetFileInformationByHandle(h, ctypes.byref(info)):
                    raise ctypes.WinError(ctypes.get_last_error())
                if info.dwFileAttributes & _REPARSE_ATTR or not info.dwFileAttributes & _ATTR_DIRECTORY:
                    raise PathPolicyError(f"not a plain directory: {cur}")
                final = _final_path(h)
                if not _contained(str(cur), final) or not _contained(final.removeprefix("\\\\?\\"), str(cur)):
                    raise PathPolicyError(f"directory changed while opening: {cur}")
            else:
                st = os.lstat(cur)
                if _is_link(st) or not stat_mod.S_ISDIR(st.st_mode):
                    raise PathPolicyError(f"not a plain directory: {cur}")
        yield str(absolute)


@contextmanager
def guarded_parent(root, rel, *, create=False):
    """Validate a relative name and retain the complete parent chain."""
    parts = _rel_parts(rel)
    with directory_guard(root) as absolute:
        parent = os.path.join(absolute, *parts[:-1])
        with directory_guard(parent, create=create):
            yield os.path.join(parent, parts[-1])


def _contained(root_real: str, final: str) -> bool:
    if final.startswith("\\\\?\\UNC\\"):
        final = "\\\\" + final[8:]
    elif final.startswith("\\\\?\\"):
        final = final[4:]
    return is_within(Path(final), Path(root_real))


if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes as _wt

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.argtypes = [_wt.LPCWSTR, _wt.DWORD, _wt.DWORD, ctypes.c_void_p, _wt.DWORD, _wt.DWORD, _wt.HANDLE]
    _k32.CreateFileW.restype = _wt.HANDLE
    _k32.CloseHandle.argtypes = [_wt.HANDLE]
    _k32.GetFinalPathNameByHandleW.argtypes = [_wt.HANDLE, _wt.LPWSTR, _wt.DWORD, _wt.DWORD]
    _k32.GetFinalPathNameByHandleW.restype = _wt.DWORD
    _k32.GetFileInformationByHandle.argtypes = [_wt.HANDLE, ctypes.c_void_p]

    class _BHFI(ctypes.Structure):
        _fields_ = [("dwFileAttributes", _wt.DWORD), ("ftCreationTime", _wt.FILETIME),
                    ("ftLastAccessTime", _wt.FILETIME), ("ftLastWriteTime", _wt.FILETIME),
                    ("dwVolumeSerialNumber", _wt.DWORD), ("nFileSizeHigh", _wt.DWORD), ("nFileSizeLow", _wt.DWORD),
                    ("nNumberOfLinks", _wt.DWORD), ("nFileIndexHigh", _wt.DWORD), ("nFileIndexLow", _wt.DWORD)]

    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _SHARE_ALL = 0x7
    _OPEN_EXISTING = 3
    _OPEN_ALWAYS = 4
    _FLAG_OPEN_REPARSE = 0x00200000
    _FLAG_BACKUP = 0x02000000
    _ATTR_DIRECTORY = 0x10
    _INVALID = ctypes.c_void_p(-1).value

    def _final_path(h: int) -> str:
        buf = ctypes.create_unicode_buffer(32768)
        n = _k32.GetFinalPathNameByHandleW(h, buf, len(buf), 0)
        if not n:
            raise OSError(ctypes.get_last_error(), "GetFinalPathNameByHandleW failed")
        return buf.value

    def _open_handle(path: str, root_real: str, *, write: bool) -> tuple[int, int]:
        """(handle, size). The caller owns the handle."""
        access = _GENERIC_WRITE | _GENERIC_READ if write else _GENERIC_READ
        h = _k32.CreateFileW(path, access, _SHARE_ALL, None, _OPEN_ALWAYS if write else _OPEN_EXISTING,
                             _FLAG_OPEN_REPARSE | _FLAG_BACKUP, None)
        if h == _INVALID or not h:
            err = ctypes.get_last_error()
            if err in (2, 3):
                raise FileNotFoundError(err, "file not found", path)
            raise OSError(err, ctypes.FormatError(err).strip(), path)
        try:
            info = _BHFI()
            if not _k32.GetFileInformationByHandle(h, ctypes.byref(info)):
                raise OSError(ctypes.get_last_error(), "GetFileInformationByHandle failed", path)
            if info.dwFileAttributes & _REPARSE_ATTR:
                raise PathPolicyError("the file is a junction/symlink")
            if info.dwFileAttributes & _ATTR_DIRECTORY:
                raise IsADirectoryError(path)
            if info.nNumberOfLinks > 1:
                raise PathPolicyError("the file is hardlinked; refusing to use it")
            if not _contained(root_real, _final_path(h)):
                raise PathPolicyError("the opened file is outside the directory")
            return h, (info.nFileSizeHigh << 32) | info.nFileSizeLow
        except BaseException:
            _k32.CloseHandle(h)
            raise

    def _open_fd(path: str, root_real: str, *, write: bool) -> tuple[int, int]:
        h, size = _open_handle(path, root_real, write=write)
        try:
            return msvcrt.open_osfhandle(h, os.O_RDWR | os.O_BINARY if write else os.O_RDONLY | os.O_BINARY), size
        except BaseException:
            _k32.CloseHandle(h)
            raise
else:
    def _open_fd(path: str, root_real: str, *, write: bool) -> tuple[int, int]:
        import stat as stat_mod

        flags = (os.O_RDWR | os.O_CREAT if write else os.O_RDONLY) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o644)
        try:
            st = os.fstat(fd)
            if not stat_mod.S_ISREG(st.st_mode):
                raise PathPolicyError("not a regular file")
            if st.st_nlink > 1:
                raise PathPolicyError("the file is hardlinked; refusing to use it")
            return fd, st.st_size
        except BaseException:
            os.close(fd)
            raise


def open_inside(root: str | os.PathLike[str], rel: str | os.PathLike[str]):  # -> BinaryIO
    """Open `root/rel` for reading without following any reparse point (see above). Returns a binary file
    object. Raises PathPolicyError (link, hardlink, escape), FileNotFoundError, IsADirectoryError, OSError."""
    with guarded_parent(root, rel) as path:
        fd, _size = _open_fd(path, os.path.abspath(root), write=False)
        return os.fdopen(fd, "rb")


def read_inside(root: str | os.PathLike[str], rel: str | os.PathLike[str], *, max_bytes: int) -> bytes:
    with guarded_parent(root, rel) as path:
        fd, size = _open_fd(path, os.path.abspath(root), write=False)
        with os.fdopen(fd, "rb") as fh:
            if size > max_bytes:
                raise FileTooLarge(size, max_bytes)
            data = fh.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise FileTooLarge(len(data), max_bytes)
        return data


def file_size_inside(root: str | os.PathLike[str], rel: str | os.PathLike[str]) -> int | None:
    """Return a safe regular file's size, or None when inaccessible."""
    try:
        with guarded_parent(root, rel) as path:
            fd, size = _open_fd(path, os.path.abspath(root), write=False)
            os.close(fd)
            return size
    except (OSError, PathPolicyError):
        return None


def make_dir_inside(root: str | os.PathLike[str], rel: str | os.PathLike[str]) -> None:
    """Create plain directories while their ancestors remain pinned."""
    with guarded_parent(root, rel, create=True) as path, directory_guard(path, create=True):
        pass


def write_inside(root: str | os.PathLike[str], rel: str | os.PathLike[str], data: bytes) -> None:
    """Create/write a regular file while every parent remains pinned."""
    with guarded_parent(root, rel, create=True) as path:
        fd, _size = _open_fd(path, os.path.abspath(root), write=True)
        with os.fdopen(fd, "r+b") as fh:
            fh.write(data)
            fh.truncate()
