"""Owner-only permissions for the orchestrator's private directories and files (security finding #8).

Windows: the DACL is replaced with explicit, non-inherited full-control entries for the current user, SYSTEM and
the Administrators group, using ``%SystemRoot%\\System32\\icacls.exe`` (argument list, no shell). The DACL is read
back through the Win32 API (``GetNamedSecurityInfoW``), so the check never parses localized ``icacls`` output.
POSIX: ``chmod 0o700`` for directories, ``0o600`` for files.

What this protects against: OTHER local accounts (e.g. ``Authenticated Users`` having Modify on a data drive).
It does NOT protect against processes running as the same user, which includes every provider CLI the
orchestrator starts. Keeping agents away from the token is the job of the provider confinement and of keeping
agent working directories outside the data directory (``providers/process.py``), not of these ACLs.

Hardening never raises: a failure is logged at ERROR level and reported by :func:`check_private`, which
``orchestrator doctor`` shows.
"""

from __future__ import annotations

import logging
import os
import stat
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

SID_SYSTEM = "S-1-5-18"
SID_ADMINISTRATORS = "S-1-5-32-544"
SID_CREATOR_OWNER = "S-1-3-0"  # inherit-only placeholder: rights for whoever creates a child (needs create rights first)
SID_OWNER_RIGHTS = "S-1-3-4"  # the object's owner; trusted only when the owner itself is trusted
# Well-known broad principals: any ACE for one of these means other local accounts can reach the path.
BROAD_SIDS = {
    "S-1-1-0": "Everyone",
    "S-1-5-11": "Authenticated Users",
    "S-1-5-32-545": "Users",
    "S-1-5-32-546": "Guests",
    "S-1-5-4": "Interactive",
    "S-1-2-0": "Local",
}

ENV_HARDEN = "ORCHESTRATOR_HARDEN_ACL"  # "0" disables hardening (the test suite sets it to keep runs fast)


def hardening_enabled() -> bool:
    return os.environ.get(ENV_HARDEN, "1") != "0"


# --------------------------------------------------------------------------- Win32 helpers

if IS_WINDOWS:  # pragma: no branch - module-level platform split
    import ctypes
    from ctypes import wintypes

    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p

    _advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL
    _advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                              ctypes.POINTER(wintypes.DWORD)]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL
    _advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    _advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    _advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
    ]
    _advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    _advapi32.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD),
                                                       ctypes.POINTER(wintypes.DWORD)]
    _advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
    _advapi32.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    _advapi32.GetAce.restype = wintypes.BOOL

    class _ACL(ctypes.Structure):
        _fields_ = [("AclRevision", ctypes.c_ubyte), ("Sbz1", ctypes.c_ubyte), ("AclSize", wintypes.WORD),
                    ("AceCount", wintypes.WORD), ("Sbz2", wintypes.WORD)]

    class _ACE_HEADER(ctypes.Structure):
        _fields_ = [("AceType", ctypes.c_ubyte), ("AceFlags", ctypes.c_ubyte), ("AceSize", wintypes.WORD)]

    _TOKEN_QUERY = 0x0008
    _TOKEN_USER = 1
    _SE_FILE_OBJECT = 1
    _OWNER_SECURITY_INFORMATION = 0x00000001
    _DACL_SECURITY_INFORMATION = 0x00000004
    _SE_DACL_PROTECTED = 0x1000
    _ACCESS_ALLOWED_ACE_TYPE = 0
    _ACCESS_DENIED_ACE_TYPE = 1
    _INHERITED_ACE = 0x10


def _sid_to_str(psid: int) -> str:
    out = wintypes.LPWSTR()
    if not _advapi32.ConvertSidToStringSidW(psid, ctypes.byref(out)):
        raise OSError(ctypes.get_last_error(), "ConvertSidToStringSidW failed")
    try:
        return str(out.value)
    finally:
        _kernel32.LocalFree(ctypes.cast(out, ctypes.c_void_p))


def current_user_sid() -> str:
    """String SID of the user this process runs as (Windows only)."""
    if not IS_WINDOWS:
        raise OSError("SIDs exist only on Windows")
    token = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise OSError(ctypes.get_last_error(), "OpenProcessToken failed")
    try:
        size = wintypes.DWORD(0)
        _advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value)
        if not _advapi32.GetTokenInformation(token, _TOKEN_USER, buf, size, ctypes.byref(size)):
            raise OSError(ctypes.get_last_error(), "GetTokenInformation failed")
        psid = ctypes.c_void_p.from_buffer(buf).value  # TOKEN_USER.User.Sid is the first field
        return _sid_to_str(psid)
    finally:
        _kernel32.CloseHandle(token)


def read_dacl(path: str | os.PathLike[str]) -> tuple[bool, list[dict]] | None:
    """(protected, aces) for `path`, or None for a NULL DACL (everyone has full access)."""
    sec = read_security(path)
    return None if sec["aces"] is None else (sec["protected"], sec["aces"])


def read_security(path: str | os.PathLike[str]) -> dict:
    """{"owner": str | None, "protected": bool, "aces": list | None (None = NULL DACL)}.

    Each ace: {"type": int, "sid": str | None, "mask": int, "inherited": bool, "flags": int}."""
    pdacl = ctypes.c_void_p()
    psd = ctypes.c_void_p()
    powner = ctypes.c_void_p()
    err = _advapi32.GetNamedSecurityInfoW(os.fspath(path), _SE_FILE_OBJECT,
                                          _DACL_SECURITY_INFORMATION | _OWNER_SECURITY_INFORMATION,
                                          ctypes.byref(powner), None, ctypes.byref(pdacl), None, ctypes.byref(psd))
    if err != 0:
        raise OSError(err, f"GetNamedSecurityInfoW failed for {path}")
    try:
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not _advapi32.GetSecurityDescriptorControl(psd, ctypes.byref(control), ctypes.byref(revision)):
            raise OSError(ctypes.get_last_error(), "GetSecurityDescriptorControl failed")
        protected = bool(control.value & _SE_DACL_PROTECTED)
        owner = _sid_to_str(powner.value) if powner.value else None
        if not pdacl.value:
            return {"owner": owner, "protected": protected, "aces": None}
        acl = _ACL.from_address(pdacl.value)
        aces: list[dict] = []
        for i in range(acl.AceCount):
            pace = ctypes.c_void_p()
            if not _advapi32.GetAce(pdacl, i, ctypes.byref(pace)):
                raise OSError(ctypes.get_last_error(), "GetAce failed")
            hdr = _ACE_HEADER.from_address(pace.value)
            sid: str | None = None
            mask = 0
            if hdr.AceType in (_ACCESS_ALLOWED_ACE_TYPE, _ACCESS_DENIED_ACE_TYPE):
                mask = ctypes.c_uint32.from_address(pace.value + 4).value
                sid = _sid_to_str(pace.value + 8)  # ACCESS_*_ACE: header(4) + mask(4) + SidStart
            aces.append({"type": hdr.AceType, "sid": sid, "mask": mask,
                         "inherited": bool(hdr.AceFlags & _INHERITED_ACE), "flags": hdr.AceFlags})
        return {"owner": owner, "protected": protected, "aces": aces}
    finally:
        _kernel32.LocalFree(psd)


# --------------------------------------------------------------------------- icacls


def icacls_path() -> str:
    root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or r"C:\Windows"
    return str(Path(root) / "System32" / "icacls.exe")


def _icacls(args: list[str]) -> None:
    proc = subprocess.run([icacls_path(), *args], stdin=subprocess.DEVNULL, capture_output=True, timeout=60,
                          check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()[:300]
        raise OSError(f"icacls {' '.join(args[1:3])} exited {proc.returncode}: {detail}")


def _trusted_sids() -> set[str]:
    return {current_user_sid(), SID_SYSTEM, SID_ADMINISTRATORS}


def _harden_windows(path: Path, *, is_dir: bool) -> None:
    inherit = "(OI)(CI)" if is_dir else ""
    sids = [current_user_sid(), SID_SYSTEM, SID_ADMINISTRATORS]
    args = [str(path), "/inheritance:r"]
    for sid in sids:
        args += ["/grant:r", f"*{sid}:{inherit}F"]
    _icacls(args)
    # /inheritance:r drops inherited entries and /grant:r replaces ours; explicit entries for anybody else stay,
    # so remove them one by one (SIDs read through the Win32 API, never parsed from localized text).
    dacl = read_dacl(path)
    trusted = set(sids)
    others = sorted({a["sid"] for a in (dacl[1] if dacl else []) if a["sid"] and a["sid"] not in trusted})
    for sid in others:
        _icacls([str(path), "/remove", f"*{sid}"])


def _harden(path: Path, *, is_dir: bool) -> bool:
    try:
        if IS_WINDOWS:
            _harden_windows(path, is_dir=is_dir)
        else:
            os.chmod(path, 0o700 if is_dir else 0o600)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.error("SECURITY: could not restrict permissions of %s to the current user: %s. Other local accounts may "
                  "be able to read it; run `orchestrator doctor`.", path, exc)
        return False
    problems = check_private(path)
    if problems:
        log.error("SECURITY: %s is still not private after hardening: %s", path, "; ".join(problems))
        return False
    return True


def harden_private_dir(path: str | os.PathLike[str]) -> bool:
    """Make `path` (an existing directory) owner-only. True on success; never raises."""
    return _harden(Path(path), is_dir=True)


def harden_private_file(path: str | os.PathLike[str]) -> bool:
    """Make `path` (an existing file) owner-only. True on success; never raises."""
    return _harden(Path(path), is_dir=False)


def check_private(path: str | os.PathLike[str]) -> list[str]:
    """Problems that let other local accounts reach `path`; [] when it is owner-only. Never raises."""
    p = Path(path)
    if not p.exists():
        return [f"{p} does not exist"]
    if not IS_WINDOWS:
        try:
            mode = stat.S_IMODE(p.stat().st_mode)
        except OSError as exc:
            return [f"cannot stat {p}: {exc}"]
        return [f"mode {oct(mode)} gives group/other access"] if mode & 0o077 else []
    try:
        sec = read_security(p)
        trusted = _trusted_sids()
    except OSError as exc:
        return [f"cannot read the ACL of {p}: {exc} (file system without ACL support?)"]
    if sec["aces"] is None:
        return ["NULL DACL: everyone has full access"]
    protected, aces = sec["protected"], sec["aces"]
    problems: list[str] = []
    if sec["owner"] in trusted:
        trusted |= {SID_OWNER_RIGHTS, SID_CREATOR_OWNER}
    else:
        problems.append(f"owned by {sec['owner']}, not by the current user")
    if not protected:
        problems.append("inherits permissions from its parent (inheritance not disabled)")
    for ace in aces:
        if ace["type"] == _ACCESS_DENIED_ACE_TYPE:
            continue
        if ace["type"] != _ACCESS_ALLOWED_ACE_TYPE:
            problems.append(f"unrecognised allow entry (ACE type {ace['type']})")
            continue
        sid = ace["sid"]
        if sid in trusted:
            continue
        name = BROAD_SIDS.get(sid or "")
        label = f"{name} ({sid})" if name else str(sid)
        problems.append(f"{'inherited ' if ace['inherited'] else ''}access granted to {label}")
    return list(dict.fromkeys(problems))
