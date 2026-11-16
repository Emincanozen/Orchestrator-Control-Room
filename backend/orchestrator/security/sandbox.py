"""Low-integrity sandbox for verification commands (security finding N2 / #5).

Verification executes code an agent wrote (a ``conftest.py``, an npm script, ``build.rs``...). Every such
command is started here with a **Low mandatory integrity level** primary token of the same user:

* the current process token is reduced with ``CreateRestrictedToken(DISABLE_MAX_PRIVILEGE)`` (every privilege
  except SeChangeNotify removed), every high-privilege group present (Administrators, the operator groups,
  domain admin groups) is made deny-only, and its integrity level is set to Low (``S-1-16-4096``);
* the process is created SUSPENDED with ``CreateProcessAsUserW`` (allowed without any privilege because the
  token is a restricted copy of the caller's own token) on a desktop created for this command only, only the
  stdin/stdout handles are inherited (``PROC_THREAD_ATTRIBUTE_HANDLE_LIST``), it is assigned to the worker Job
  Object and to a per-command job nested in it (UI limits: no clipboard, no foreign USER handles, no global
  atoms, no desktop switch, no system parameters, no logoff; everything left at the end of the command is
  terminated), its integrity level is read back, and only then it is resumed.

Windows mandatory integrity control then enforces *no-write-up*: a Low process cannot open for writing (or
delete, rename, change the DACL/label of) any object whose label is Medium - and an object without an explicit
label counts as Medium with NO_WRITE_UP. That covers the user profile, ``%TEMP%``, the repository and its
``.git``, the orchestrator source, venv and ``config/``, and the data directory. The data directory additionally
carries an explicit Medium label with NO_READ_UP | NO_EXECUTE_UP (``protect_data_dir``; checked before every
command, fail closed), so a Low process cannot even read the database or the API token.

Writable for the sandbox: a disposable per-command copy of the task worktree (``verify_tree``), the per-command
cache dir and temp dir, all inside one per-run directory that is removed after the command. The copy is granted
to the run's AppContainer identity and labelled Low (inheritable) while still empty; the run directory and its
parents stay Medium, so they cannot be swapped for junctions. The real task worktree is never granted, labelled
or written: nothing a command creates (files, junctions, hardlinks) can be met later by orchestrator git, a
provider run, a commit or another verification. ``in_place`` projects are refused (see ``Session``).

Around every command (``Session``): the copy's ``.git`` link and the agent instruction / configuration files
(``agent_files``) are snapshotted; a missing/non-regular ``.git`` before the command, or any change to them by
the command, is restored and reported as ``sandbox_tamper`` - a security finding that blocks the task. Orchestrator
git never reads the ``.git`` link anyway (``gitops.git`` always passes an explicit git dir) and does not run in the
worktree while the command executes (``git.worktree_busy``).

There is no switch that turns this off. When the sandbox cannot be established (unsupported platform, token,
label, job, desktop or data-dir-label failure) ``SandboxUnavailable`` is raised and the command is NOT started.

What this does NOT block (residuals; see the tests and SECURITY_REVIEW): reading the user's other files
(default label policy is no-write-up only), network access, writing other Low objects (the contents of other
task worktrees and caches, ``%USERPROFILE%\\AppData\\LocalLow``, ``HKCU\\Software\\AppDataLow``), and anything a
Medium broker that accepts Low clients does on its behalf. A per-task AppContainer is the planned fix.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from orchestrator.security import timing

import time
from pathlib import Path
from typing import Callable, Optional, Sequence

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

LOW_SID = "S-1-16-4096"
MEDIUM_SID = "S-1-16-8192"
LOW_RID = 0x1000
MEDIUM_RID = 0x2000

# Mandatory label policies (SYSTEM_MANDATORY_LABEL_ACE mask)
NO_WRITE_UP = 0x1
NO_READ_UP = 0x2
NO_EXECUTE_UP = 0x4
# SDDL of the two labels the orchestrator applies (OICI = inherited by every file and directory created below).
SDDL_LOW_TREE = "S:(ML;OICI;NW;;;LW)"
SDDL_DATA_DIR = "S:(ML;OICI;NWNRNX;;;ME)"

ERROR_CODE = "sandbox_unavailable"
TAMPER_CODE = "sandbox_tamper"
# Verification results whose error starts with one of these are security findings: the task is blocked (never a
# repair cycle, never completable through a policy auto-approval).
SECURITY_CODES = (ERROR_CODE, TAMPER_CODE)

# Group SIDs that are made deny-only in the sandbox token (N2 re-review S7). Disabling a SID the token does not
# hold is a no-op. Domain-relative admin groups are matched by RID (see `_high_privilege_sids`).
HIGH_PRIVILEGE_SIDS = (
    "S-1-5-32-544",  # BUILTIN\Administrators
    "S-1-5-32-547",  # Power Users
    "S-1-5-32-548",  # Account Operators
    "S-1-5-32-549",  # Server Operators
    "S-1-5-32-550",  # Print Operators
    "S-1-5-32-551",  # Backup Operators
    "S-1-5-32-552",  # Replicators
    "S-1-5-32-556",  # Network Configuration Operators
    "S-1-5-32-569",  # Cryptographic Operators
    "S-1-5-32-578",  # Hyper-V Administrators
    "S-1-5-32-579",  # Access Control Assistance Operators
    "S-1-5-114",     # Local account and member of Administrators group
)
_DOMAIN_ADMIN_RIDS = ("512", "518", "519", "520")  # Domain / Schema / Enterprise Admins, GP Creator Owners

# JOBOBJECT_BASIC_UI_RESTRICTIONS of every per-command job (N2 re-review S4).
UILIMIT_HANDLES = 0x1
UILIMIT_READCLIPBOARD = 0x2
UILIMIT_WRITECLIPBOARD = 0x4
UILIMIT_SYSTEMPARAMETERS = 0x8
UILIMIT_DISPLAYSETTINGS = 0x10
UILIMIT_GLOBALATOMS = 0x20
UILIMIT_DESKTOP = 0x40
UILIMIT_EXITWINDOWS = 0x80
UI_RESTRICTIONS = (UILIMIT_HANDLES | UILIMIT_READCLIPBOARD | UILIMIT_WRITECLIPBOARD | UILIMIT_SYSTEMPARAMETERS
                   | UILIMIT_DISPLAYSETTINGS | UILIMIT_GLOBALATOMS | UILIMIT_DESKTOP | UILIMIT_EXITWINDOWS)


class SandboxUnavailable(RuntimeError):
    """The Low-integrity sandbox could not be established; the command must not run."""

    def __init__(self, detail: str):
        super().__init__(f"{ERROR_CODE}: {detail}")
        self.detail = detail


class SandboxTampered(RuntimeError):
    """The worktree's git link or agent configuration is not what the orchestrator left there (before or after a
    command). A security finding: the task is blocked."""

    def __init__(self, detail: str):
        super().__init__(f"{TAMPER_CODE}: {detail}")
        self.detail = detail


def is_security_error(error: Optional[str]) -> bool:
    return bool(error) and any(str(error).startswith(code) for code in SECURITY_CODES)


if IS_WINDOWS:  # pragma: no branch - module-level platform split
    import ctypes
    import msvcrt
    from ctypes import wintypes as wt

    _adv = ctypes.WinDLL("advapi32", use_last_error=True)
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    TOKEN_ASSIGN_PRIMARY = 0x0001
    TOKEN_DUPLICATE = 0x0002
    TOKEN_QUERY = 0x0008
    TOKEN_ADJUST_DEFAULT = 0x0080
    TOKEN_ALL_ACCESS = 0xF01FF
    DISABLE_MAX_PRIVILEGE = 0x1
    TokenIntegrityLevel = 25
    TokenPrivileges = 3
    SE_GROUP_INTEGRITY = 0x20
    SE_FILE_OBJECT = 1
    LABEL_SECURITY_INFORMATION = 0x10
    SYSTEM_MANDATORY_LABEL_ACE_TYPE = 0x11
    CREATE_SUSPENDED = 0x4
    CREATE_NEW_PROCESS_GROUP = 0x200
    CREATE_UNICODE_ENVIRONMENT = 0x400
    EXTENDED_STARTUPINFO_PRESENT = 0x80000
    CREATE_NO_WINDOW = 0x08000000
    STARTF_USESTDHANDLES = 0x100
    PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x20002
    DUPLICATE_SAME_ACCESS = 0x2
    WAIT_OBJECT_0 = 0
    WAIT_TIMEOUT = 0x102
    STILL_ACTIVE = 259
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_TERMINATE = 0x0001
    PROCESS_SET_QUOTA = 0x0100

    class _STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR), ("lpTitle", wt.LPWSTR),
            ("dwX", wt.DWORD), ("dwY", wt.DWORD), ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
            ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD), ("dwFillAttribute", wt.DWORD),
            ("dwFlags", wt.DWORD), ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
            ("lpReserved2", ctypes.c_void_p), ("hStdInput", wt.HANDLE), ("hStdOutput", wt.HANDLE),
            ("hStdError", wt.HANDLE),
        ]

    class _STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [("StartupInfo", _STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]

    class _PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE), ("dwProcessId", wt.DWORD),
                    ("dwThreadId", wt.DWORD)]

    class _SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wt.DWORD)]

    class _ACL(ctypes.Structure):
        _fields_ = [("AclRevision", ctypes.c_ubyte), ("Sbz1", ctypes.c_ubyte), ("AclSize", wt.WORD),
                    ("AceCount", wt.WORD), ("Sbz2", wt.WORD)]

    class _ACE_HEADER(ctypes.Structure):
        _fields_ = [("AceType", ctypes.c_ubyte), ("AceFlags", ctypes.c_ubyte), ("AceSize", wt.WORD)]

    class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [("dwFileAttributes", wt.DWORD), ("ftCreationTime", wt.FILETIME), ("ftLastAccessTime", wt.FILETIME),
                    ("ftLastWriteTime", wt.FILETIME), ("dwVolumeSerialNumber", wt.DWORD), ("nFileSizeHigh", wt.DWORD),
                    ("nFileSizeLow", wt.DWORD), ("nNumberOfLinks", wt.DWORD), ("nFileIndexHigh", wt.DWORD),
                    ("nFileIndexLow", wt.DWORD)]

    READ_CONTROL = 0x00020000
    WRITE_OWNER = 0x00080000
    FILE_READ_ATTRIBUTES = 0x80
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    def _sig(fn, argtypes, restype=wt.BOOL):
        fn.argtypes = argtypes
        fn.restype = restype

    _sig(_k32.GetCurrentProcess, [], wt.HANDLE)
    _sig(_k32.CloseHandle, [wt.HANDLE])
    _sig(_k32.LocalFree, [ctypes.c_void_p], ctypes.c_void_p)
    _sig(_k32.InitializeProcThreadAttributeList, [ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.c_size_t)])
    _sig(_k32.UpdateProcThreadAttribute, [ctypes.c_void_p, wt.DWORD, ctypes.c_size_t, ctypes.c_void_p,
                                          ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p])
    _sig(_k32.DeleteProcThreadAttributeList, [ctypes.c_void_p], None)
    _sig(_k32.DuplicateHandle, [wt.HANDLE, wt.HANDLE, wt.HANDLE, ctypes.POINTER(wt.HANDLE), wt.DWORD, wt.BOOL,
                                wt.DWORD])
    _sig(_k32.WaitForSingleObject, [wt.HANDLE, wt.DWORD], wt.DWORD)
    _sig(_k32.GetExitCodeProcess, [wt.HANDLE, ctypes.POINTER(wt.DWORD)])
    _sig(_k32.TerminateProcess, [wt.HANDLE, wt.UINT])
    _sig(_k32.OpenProcess, [wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE)
    _sig(_k32.CreateFileW, [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE],
         wt.HANDLE)
    _sig(_k32.GetFileInformationByHandle, [wt.HANDLE, ctypes.c_void_p])
    _sig(_adv.SetKernelObjectSecurity, [wt.HANDLE, wt.DWORD, ctypes.c_void_p])
    _sig(_k32.CreateJobObjectW, [ctypes.c_void_p, wt.LPCWSTR], wt.HANDLE)
    _sig(_k32.AssignProcessToJobObject, [wt.HANDLE, wt.HANDLE])
    _sig(_k32.TerminateJobObject, [wt.HANDLE, wt.UINT])
    _sig(_adv.OpenProcessToken, [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)])
    _sig(_adv.CreateRestrictedToken, [wt.HANDLE, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p,
                                      wt.DWORD, ctypes.c_void_p, ctypes.POINTER(wt.HANDLE)])
    _sig(_adv.SetTokenInformation, [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD])
    _sig(_adv.GetTokenInformation, [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)])
    _sig(_adv.ConvertStringSidToSidW, [wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)])
    _sig(_adv.GetLengthSid, [ctypes.c_void_p], wt.DWORD)
    _sig(_adv.GetSidSubAuthorityCount, [ctypes.c_void_p], ctypes.POINTER(ctypes.c_ubyte))
    _sig(_adv.GetSidSubAuthority, [ctypes.c_void_p, wt.DWORD], ctypes.POINTER(wt.DWORD))
    _sig(_adv.ConvertStringSecurityDescriptorToSecurityDescriptorW,
         [wt.LPCWSTR, wt.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p])
    _sig(_adv.GetSecurityDescriptorSacl, [ctypes.c_void_p, ctypes.POINTER(wt.BOOL), ctypes.POINTER(ctypes.c_void_p),
                                          ctypes.POINTER(wt.BOOL)])
    _sig(_adv.SetNamedSecurityInfoW, [wt.LPWSTR, ctypes.c_int, wt.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_void_p, ctypes.c_void_p], wt.DWORD)
    _sig(_adv.GetNamedSecurityInfoW, [wt.LPCWSTR, ctypes.c_int, wt.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                                      ctypes.POINTER(ctypes.c_void_p)], wt.DWORD)
    _sig(_adv.GetAce, [ctypes.c_void_p, wt.DWORD, ctypes.POINTER(ctypes.c_void_p)])
    _sig(_adv.CreateProcessAsUserW, [wt.HANDLE, wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wt.BOOL,
                                     wt.DWORD, ctypes.c_void_p, wt.LPCWSTR, ctypes.c_void_p,
                                     ctypes.POINTER(_PROCESS_INFORMATION)])
    _sig(_adv.GetKernelObjectSecurity, [wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)])
    _sig(_adv.ConvertSecurityDescriptorToStringSecurityDescriptorW,
         [ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.POINTER(wt.LPWSTR), ctypes.c_void_p])
    _sig(_adv.ConvertSidToStringSidW, [ctypes.c_void_p, ctypes.POINTER(wt.LPWSTR)])
    _sig(_k32.SetInformationJobObject, [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD])
    _sig(_k32.QueryInformationJobObject, [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD,
                                          ctypes.POINTER(wt.DWORD)])

    _u32 = ctypes.WinDLL("user32", use_last_error=True)
    _sig(_u32.CreateDesktopW, [wt.LPCWSTR, wt.LPCWSTR, ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.c_void_p],
         wt.HANDLE)
    _sig(_u32.CloseDesktop, [wt.HANDLE])
    _sig(_u32.GetProcessWindowStation, [], wt.HANDLE)
    _sig(_u32.GetUserObjectInformationW, [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD,
                                          ctypes.POINTER(wt.DWORD)])

    class _SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("nLength", wt.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", wt.BOOL)]

    TokenUser = 1
    TokenGroups = 2
    TokenElevation = 20
    SE_GROUP_ENABLED = 0x4
    SE_GROUP_USE_FOR_DENY_ONLY = 0x10
    DACL_SECURITY_INFORMATION = 0x4
    WRITE_DAC = 0x00040000
    DELETE = 0x00010000
    JobObjectBasicUIRestrictions = 4
    GENERIC_ALL = 0x10000000

    def _token_info(token: int, cls: int) -> ctypes.Array:
        size = wt.DWORD(0)
        _adv.GetTokenInformation(token, cls, None, 0, ctypes.byref(size))
        if not size.value:
            raise _winerr(f"GetTokenInformation({cls}) size query")
        # Fixed-size classes (notably TokenElevation) reject an oversized buffer
        # with ERROR_BAD_LENGTH. Use the exact size returned by Windows.
        buf = ctypes.create_string_buffer(size.value)
        if not _adv.GetTokenInformation(token, cls, buf, len(buf), ctypes.byref(size)):
            raise _winerr(f"GetTokenInformation({cls})")
        return buf

    def _sid_str(psid: int) -> str:
        s = wt.LPWSTR()
        if not _adv.ConvertSidToStringSidW(psid, ctypes.byref(s)):
            raise _winerr("ConvertSidToStringSidW")
        try:
            return s.value or ""
        finally:
            _k32.LocalFree(ctypes.cast(s, ctypes.c_void_p))

    def _token_groups(token: int) -> tuple[ctypes.Array, list[tuple[str, int, int]]]:
        """(buffer, [(SID string, attributes, SID address)]): the addresses point into the buffer, which the
        caller keeps alive while it uses them."""
        buf = _token_info(token, TokenGroups)
        count = ctypes.c_uint32.from_buffer(buf).value
        arr = ctypes.cast(ctypes.addressof(buf) + ctypes.sizeof(ctypes.c_void_p),
                          ctypes.POINTER(_SID_AND_ATTRIBUTES))
        return buf, [(_sid_str(arr[i].Sid), arr[i].Attributes, arr[i].Sid) for i in range(count)]

    def _process_token(access: int) -> int:
        h = wt.HANDLE()
        if not _adv.OpenProcessToken(_k32.GetCurrentProcess(), access, ctypes.byref(h)):
            raise _winerr("OpenProcessToken")
        return h.value

    def current_user_sid() -> str:
        tok = _process_token(TOKEN_QUERY)
        try:
            buf = _token_info(tok, TokenUser)
            return _sid_str(ctypes.c_void_p.from_buffer(buf).value)
        finally:
            _k32.CloseHandle(tok)

    def _winerr(what: str, code: Optional[int] = None) -> OSError:
        code = ctypes.get_last_error() if code is None else code
        return OSError(code, f"{what} failed: {ctypes.FormatError(code).strip()} (error {code})")

    # ----------------------------------------------------------------------------- labels

    def _apply_label(path: str, sddl: str, *, tree: bool = True) -> None:
        """tree=True: SetNamedSecurityInfoW, which propagates the inheritable label to every existing object below
        (it does NOT follow junctions, but it DOES relabel hardlinked files, whose security descriptor is shared
        with the link target outside the tree). tree=False: the directory itself only, through a handle opened
        without following reparse points (SetKernelObjectSecurity never propagates); objects created below it
        later inherit the label."""
        psd = ctypes.c_void_p()
        if not _adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(psd), None):
            raise _winerr("ConvertStringSecurityDescriptorToSecurityDescriptorW")
        try:
            if not tree:
                h = _k32.CreateFileW(path, READ_CONTROL | WRITE_OWNER | FILE_READ_ATTRIBUTES, 7, None, OPEN_EXISTING,
                                     FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, None)
                if h == INVALID_HANDLE_VALUE or not h:
                    raise _winerr(f"CreateFileW({path})")
                try:
                    info = _BY_HANDLE_FILE_INFORMATION()
                    if not _k32.GetFileInformationByHandle(h, ctypes.byref(info)):
                        raise _winerr("GetFileInformationByHandle")
                    if info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
                        raise OSError(f"{path} is a reparse point (junction/symlink); refusing to label it")
                    if not _adv.SetKernelObjectSecurity(h, LABEL_SECURITY_INFORMATION, psd):
                        raise _winerr(f"SetKernelObjectSecurity({path})")
                finally:
                    _k32.CloseHandle(h)
                return
            present, defaulted = wt.BOOL(), wt.BOOL()
            psacl = ctypes.c_void_p()
            if not _adv.GetSecurityDescriptorSacl(psd, ctypes.byref(present), ctypes.byref(psacl), ctypes.byref(defaulted)):
                raise _winerr("GetSecurityDescriptorSacl")
            err = _adv.SetNamedSecurityInfoW(path, SE_FILE_OBJECT, LABEL_SECURITY_INFORMATION, None, None, None, psacl)
            if err != 0:
                raise _winerr(f"SetNamedSecurityInfoW({path})", err)
        finally:
            _k32.LocalFree(psd)

    def _sid_rid(psid: int) -> int:
        count = _adv.GetSidSubAuthorityCount(psid).contents.value
        return _adv.GetSidSubAuthority(psid, count - 1).contents.value

    def _read_label(path: str) -> Optional[tuple[int, int, int]]:
        psacl = ctypes.c_void_p()
        psd = ctypes.c_void_p()
        err = _adv.GetNamedSecurityInfoW(path, SE_FILE_OBJECT, LABEL_SECURITY_INFORMATION, None, None, None,
                                         ctypes.byref(psacl), ctypes.byref(psd))
        if err != 0:
            raise _winerr(f"GetNamedSecurityInfoW({path})", err)
        try:
            if not psacl.value:
                return None
            acl = _ACL.from_address(psacl.value)
            for i in range(acl.AceCount):
                pace = ctypes.c_void_p()
                if not _adv.GetAce(psacl, i, ctypes.byref(pace)):
                    raise _winerr("GetAce")
                hdr = _ACE_HEADER.from_address(pace.value)
                if hdr.AceType == SYSTEM_MANDATORY_LABEL_ACE_TYPE:
                    mask = ctypes.c_uint32.from_address(pace.value + 4).value
                    return _sid_rid(pace.value + 8), mask, hdr.AceFlags
            return None
        finally:
            _k32.LocalFree(psd)

    # ----------------------------------------------------------------------------- token

    def _token_integrity_rid(token: int) -> int:
        size = wt.DWORD(0)
        _adv.GetTokenInformation(token, TokenIntegrityLevel, None, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(max(size.value, 64))
        if not _adv.GetTokenInformation(token, TokenIntegrityLevel, buf, len(buf), ctypes.byref(size)):
            raise _winerr("GetTokenInformation(TokenIntegrityLevel)")
        return _sid_rid(ctypes.c_void_p.from_buffer(buf).value)

    def _create_low_token() -> int:
        """A primary token: the caller's own token, all privileges but SeChangeNotify removed, every
        high-privilege group deny-only, Low integrity."""
        base = wt.HANDLE()
        if not _adv.OpenProcessToken(_k32.GetCurrentProcess(),
                                     TOKEN_DUPLICATE | TOKEN_QUERY | TOKEN_ASSIGN_PRIMARY | TOKEN_ADJUST_DEFAULT,
                                     ctypes.byref(base)):
            raise _winerr("OpenProcessToken")
        try:
            keep, groups = _token_groups(base.value)  # `keep` owns the memory the SID addresses point into
            high = set(_high_privilege_sids([g[0] for g in groups]))
            disable = [addr for sid, _attrs, addr in groups if sid in high]
            arr = (_SID_AND_ATTRIBUTES * max(1, len(disable)))()
            for i, addr in enumerate(disable):
                arr[i].Sid = addr
                arr[i].Attributes = 0
            low = wt.HANDLE()
            ok = _adv.CreateRestrictedToken(base, DISABLE_MAX_PRIVILEGE, len(disable), arr if disable else None,
                                            0, None, 0, None, ctypes.byref(low))
            del keep
            if not ok:
                raise _winerr("CreateRestrictedToken")
        finally:
            _k32.CloseHandle(base)
        psid = ctypes.c_void_p()
        try:
            if not _adv.ConvertStringSidToSidW(LOW_SID, ctypes.byref(psid)):
                raise _winerr("ConvertStringSidToSidW")
            tml = _SID_AND_ATTRIBUTES(psid.value, SE_GROUP_INTEGRITY)
            size = ctypes.sizeof(tml) + _adv.GetLengthSid(psid)
            if not _adv.SetTokenInformation(low, TokenIntegrityLevel, ctypes.byref(tml), size):
                raise _winerr("SetTokenInformation(TokenIntegrityLevel)")
            if _token_integrity_rid(low.value) != LOW_RID:  # read back: never run with a token we did not verify
                raise OSError("the token integrity level could not be verified as Low")
        except BaseException:
            _k32.CloseHandle(low)
            raise
        finally:
            if psid.value:
                _k32.LocalFree(psid)
        return low.value

    def _inheritable_dup(handle: int) -> int:
        out = wt.HANDLE()
        cur = _k32.GetCurrentProcess()
        if not _k32.DuplicateHandle(cur, handle, cur, ctypes.byref(out), 0, True, DUPLICATE_SAME_ACCESS):
            raise _winerr("DuplicateHandle")
        return out.value


# --------------------------------------------------------------------------------- public API: labels


def supported() -> bool:
    return IS_WINDOWS


def _high_privilege_sids(sids: Sequence[str]) -> list[str]:
    """The SIDs among `sids` that the sandbox token must hold as deny-only."""
    out = []
    for sid in sids:
        if sid in HIGH_PRIVILEGE_SIDS or (sid.startswith("S-1-5-21-") and sid.rsplit("-", 1)[-1] in _DOMAIN_ADMIN_RIDS):
            out.append(sid)
    return out


def _low_label_ok(path: str) -> bool:
    try:
        label = _read_label(path)
    except OSError:
        return False
    return label is not None and label[0] == LOW_RID and bool(label[1] & NO_WRITE_UP) and label[2] & 0x3 == 0x3


def label_low(path: str | os.PathLike[str], *, tree: bool = False) -> None:
    """Give `path` (a directory the sandbox may write) an inheritable Low label. Raises SandboxUnavailable.

    tree=False (default, every re-assertion): the directory only; what is created below it inherits the label.
    A directory that already carries exactly this label is left alone (a pinned directory denies WRITE_OWNER,
    which relabelling needs). tree=True: also relabel everything already inside. Not used for worktrees (they
    are labelled while still empty): propagation relabels hardlinked files, so on a tree an agent has touched, a
    planted hardlink to e.g. the database would make the link target writable at Low integrity (tested)."""
    if not IS_WINDOWS:
        raise SandboxUnavailable("no sandbox implementation on this platform (Windows only)")
    if not tree and _low_label_ok(os.fspath(path)):
        return
    try:
        _apply_label(os.fspath(path), SDDL_LOW_TREE, tree=tree)
    except OSError as exc:
        raise SandboxUnavailable(f"cannot label {path} for the sandbox: {exc}") from exc


def _pin_aces(user_sid: str) -> str:
    # Explicit deny of DELETE, WRITE_DAC, WRITE_OWNER for the user's own SID (every token of this user holds it,
    # Low or Medium), and OWNER RIGHTS limited to READ_CONTROL (otherwise the owner - the same user - would keep
    # an implicit WRITE_DAC and could remove the deny). Not inheritable: children stay deletable.
    return f"(D;;SDWDWO;;;{user_sid})(A;;RC;;;OW)"


def pin_directory(path: str | os.PathLike[str]) -> None:
    """Make `path` (a directory) undeletable / unrenamable / un-re-ACL-able for every process of this user
    (N2 re-review S5). A rename or delete needs DELETE on the object or FILE_DELETE_CHILD on its parent; the
    parent (worktrees dir, sandbox dir) is Medium, so a Low process has neither. A Medium process (the
    orchestrator) still deletes it through FILE_DELETE_CHILD on the parent. Idempotent. Raises SandboxUnavailable."""
    if not IS_WINDOWS:
        raise SandboxUnavailable("no sandbox implementation on this platform (Windows only)")
    p = os.fspath(path)
    try:
        user = current_user_sid()
        aces = _pin_aces(user)
        h = _k32.CreateFileW(p, READ_CONTROL | WRITE_DAC | FILE_READ_ATTRIBUTES, 7, None, OPEN_EXISTING,
                             FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, None)
        if h == INVALID_HANDLE_VALUE or not h:
            if is_pinned(p):  # already pinned: WRITE_DAC is denied by design
                return
            raise _winerr(f"CreateFileW({p})")
        try:
            info = _BY_HANDLE_FILE_INFORMATION()
            if not _k32.GetFileInformationByHandle(h, ctypes.byref(info)):
                raise _winerr("GetFileInformationByHandle")
            if info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
                raise OSError(f"{p} is a reparse point; refusing to pin it")
            sddl = _handle_dacl_sddl(h)
            if aces in sddl:
                return
            i = sddl.find("(")
            new = (sddl + aces) if i < 0 else sddl[:i] + aces + sddl[i:]
            psd = ctypes.c_void_p()
            if not _adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(new, 1, ctypes.byref(psd), None):
                raise _winerr("ConvertStringSecurityDescriptorToSecurityDescriptorW")
            try:
                if not _adv.SetKernelObjectSecurity(h, DACL_SECURITY_INFORMATION, psd):
                    raise _winerr(f"SetKernelObjectSecurity({p})")
            finally:
                _k32.LocalFree(psd)
        finally:
            _k32.CloseHandle(h)
    except OSError as exc:
        raise SandboxUnavailable(f"cannot pin {p}: {exc}") from exc


def _handle_dacl_sddl(h: int) -> str:
    n = wt.DWORD()
    _adv.GetKernelObjectSecurity(h, DACL_SECURITY_INFORMATION, None, 0, ctypes.byref(n))
    buf = ctypes.create_string_buffer(max(n.value, 64))
    if not _adv.GetKernelObjectSecurity(h, DACL_SECURITY_INFORMATION, buf, len(buf), ctypes.byref(n)):
        raise _winerr("GetKernelObjectSecurity")
    s = wt.LPWSTR()
    if not _adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(buf, 1, DACL_SECURITY_INFORMATION,
                                                                      ctypes.byref(s), None):
        raise _winerr("ConvertSecurityDescriptorToStringSecurityDescriptorW")
    try:
        return s.value or ""
    finally:
        _k32.LocalFree(ctypes.cast(s, ctypes.c_void_p))


def is_pinned(path: str | os.PathLike[str]) -> bool:
    if not IS_WINDOWS:
        return False
    try:
        h = _k32.CreateFileW(os.fspath(path), READ_CONTROL, 7, None, OPEN_EXISTING,
                             FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, None)
        if h == INVALID_HANDLE_VALUE or not h:
            return False
        try:
            return _pin_aces(current_user_sid()) in _handle_dacl_sddl(h)
        finally:
            _k32.CloseHandle(h)
    except OSError:
        return False


_REPARSE = 0x400


def _lstat_reparse(path: str) -> Optional[os.stat_result]:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def is_reparse(st: os.stat_result) -> bool:
    import stat as stat_mod

    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE) or stat_mod.S_ISLNK(st.st_mode)


def _unlink_entry(path: str, st: os.stat_result) -> None:
    """Remove one entry without following it: a junction / directory symlink is removed as a link."""
    import stat as stat_mod

    if stat_mod.S_ISDIR(st.st_mode) or (getattr(st, "st_file_attributes", 0) & 0x10):  # FILE_ATTRIBUTE_DIRECTORY
        try:
            os.rmdir(path)
            return
        except OSError:
            if not is_reparse(st):
                raise
    # Never chmod a path supplied by a concurrent writer: it may now be a link.
    os.unlink(path)


def remove_tree(path: str | os.PathLike[str]) -> None:
    """Remove entries without following links; retain all ancestors during traversal.

    Unsafe or inaccessible entries fail closed. Missing paths remain a no-op.
    """
    from contextlib import ExitStack
    from orchestrator.security.paths import directory_guard, PathPolicyError

    root = os.path.abspath(os.fspath(path))
    if os.path.dirname(root) == root:
        raise PathPolicyError("refusing to remove a filesystem root")
    if _lstat_reparse(root) is None:
        return
    with directory_guard(os.path.dirname(root)):
        # Each frame holds its directory until its children have been processed.
        with ExitStack() as cleanup:
            stack = [(root, False, None)]
            for _ in range(10_000_000):
                if not stack:
                    return
                current, visited, guard = stack.pop()
                if visited:
                    guard.close()
                    os.rmdir(current)  # parent is still pinned; rmdir never follows a link
                    continue
                st = _lstat_reparse(current)
                if st is None:
                    continue
                if is_reparse(st) or not _is_dir_stat(st):
                    _unlink_entry(current, st)
                    continue
                guard = ExitStack()
                cleanup.callback(guard.close)
                guard.enter_context(directory_guard(current))
                stack.append((current, True, guard))
                with os.scandir(current) as entries:
                    stack.extend((entry.path, False, None) for entry in entries)
            raise PathPolicyError("directory removal traversal limit exceeded")


def _is_dir_stat(st: os.stat_result) -> bool:
    import stat as stat_mod

    return stat_mod.S_ISDIR(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x10)


def real_dir(path: str | os.PathLike[str], *, create: bool = False) -> Path:
    """`path` as a real directory: created when `create`, refused when it is a reparse point or not a
    directory. Raises SandboxUnavailable."""
    p = os.fspath(path)
    st = _lstat_reparse(p)
    if st is None:
        if not create:
            raise SandboxUnavailable(f"{p} does not exist")
        try:
            os.mkdir(p)
        except FileExistsError:
            pass
        except OSError as exc:
            raise SandboxUnavailable(f"cannot create {p}: {exc}") from exc
        st = _lstat_reparse(p)
    if st is None or is_reparse(st) or not _is_dir_stat(st):
        raise SandboxUnavailable(f"{p} is not a plain directory (junction, symlink or file); refusing to use it")
    return Path(p)


def medium_plain_root(root: str | os.PathLike[str]) -> Path:
    """The per-project sandbox root as a Medium, plain directory (created when missing). A root left
    Low-labelled by an older version (whose children a Low process could have replaced with junctions) is
    removed without following links and rebuilt. Raises SandboxUnavailable."""
    root = Path(root)
    try:
        root.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SandboxUnavailable(f"cannot create the sandbox directory {root.parent}: {exc}") from exc
    real_dir(root.parent)
    if os.path.lexists(root):
        st = _lstat_reparse(str(root))
        legacy_low = st is not None and not is_reparse(st) and _low_label_ok(str(root))
        if st is None or is_reparse(st) or not _is_dir_stat(st) or legacy_low:
            log.warning("SECURITY: rebuilding the sandbox directory %s (not a Medium plain directory)", root)
            remove_tree(root)
    return real_dir(root, create=True)


def token_groups_of_sandbox_token() -> list[tuple[str, int]]:
    """(SID, attributes) of every group in a freshly created sandbox token (tests, doctor). Raises OSError."""
    if not IS_WINDOWS:
        raise OSError("tokens exist only on Windows")
    tok = _create_low_token()
    try:
        _buf, groups = _token_groups(tok)
        return [(sid, attrs) for sid, attrs, _addr in groups]
    finally:
        _k32.CloseHandle(tok)


ELEVATED_MESSAGE = (
    "refusing to run elevated (as administrator): the orchestrator runs agents and verification commands as "
    "children of this process, and an elevated token would give them administrator rights. Start it from a normal "
    "(non-administrator) terminal; nothing in the orchestrator needs elevation.")


class ElevatedProcess(RuntimeError):
    pass


def refuse_elevated(probe: "Optional[Callable[[], bool]]" = None) -> None:
    """Raise ElevatedProcess when this process is elevated (N2 re-review S7). No switch: never needed."""
    if (probe or is_elevated)():
        raise ElevatedProcess(ELEVATED_MESSAGE)


def is_elevated() -> bool:
    """True when this process runs with an elevated (administrator) token."""
    if not IS_WINDOWS:
        return hasattr(os, "geteuid") and os.geteuid() == 0  # type: ignore[attr-defined]
    try:
        tok = _process_token(TOKEN_QUERY)
    except OSError as exc:
        raise SandboxUnavailable(f"cannot determine whether the server is elevated: {exc}") from exc
    try:
        buf = _token_info(tok, TokenElevation)
        return ctypes.c_uint32.from_buffer(buf).value != 0
    except OSError as exc:
        raise SandboxUnavailable(f"cannot determine whether the server is elevated: {exc}") from exc
    finally:
        _k32.CloseHandle(tok)


def protect_data_dir(path: str | os.PathLike[str]) -> bool:
    """Explicit Medium label with NO_WRITE_UP | NO_READ_UP | NO_EXECUTE_UP, inherited by every file created later
    (database, WAL/SHM files, token, logs): a Low process can neither read nor write anything in it. Medium
    processes (server, CLI, provider CLIs) are unaffected. True on success; never raises (logged; doctor checks)."""
    if not IS_WINDOWS:
        return False
    try:
        _apply_label(os.fspath(path), SDDL_DATA_DIR)
        return True
    except OSError as exc:
        log.error("SECURITY: could not apply the no-read-up integrity label to %s: %s; sandboxed verification "
                  "commands could read the database and the token. Run `orchestrator doctor`.", path, exc)
        return False


def read_label(path: str | os.PathLike[str]) -> Optional[tuple[int, int, int]]:
    """(integrity RID, policy mask, ACE flags) of the explicit mandatory label, None when there is none
    (= implicit Medium, NO_WRITE_UP). Raises OSError."""
    if not IS_WINDOWS:
        raise OSError("integrity labels exist only on Windows")
    return _read_label(os.fspath(path))


def check_data_dir_label(path: str | os.PathLike[str]) -> list[str]:
    """Problems with the data dir's label for `orchestrator doctor`; [] when it is protected. Never raises."""
    if not IS_WINDOWS:
        return []
    try:
        label = read_label(path)
    except OSError as exc:
        return [f"cannot read the integrity label of {path}: {exc}"]
    if label is None:
        return [f"{path} has no explicit integrity label: sandboxed verification code could read it"]
    rid, mask, flags = label
    problems = []
    if rid < MEDIUM_RID:
        problems.append(f"integrity label of {path} is below Medium (0x{rid:x})")
    if mask & (NO_WRITE_UP | NO_READ_UP) != (NO_WRITE_UP | NO_READ_UP):
        problems.append(f"integrity label of {path} lacks no-read-up/no-write-up (policy 0x{mask:x})")
    if flags & 0x3 != 0x3:  # OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE
        problems.append(f"integrity label of {path} is not inherited by new files (flags 0x{flags:x})")
    return problems


# --------------------------------------------------------------------------------- public API: environment


def sandbox_env(env: dict[str, str], *, tmp: Path, cache: Path, read_only_cwd: bool) -> dict[str, str]:
    """Environment for a sandboxed command: temp dir and tool caches redirected into Low-labelled directories.

    TEMP/TMP: fresh per command (`tmp`), removed afterwards, so nothing survives between commands.
    Caches (`cache`, per project): npm, uv, pip, XDG. The user's own caches (Medium) are never used, so
    sandboxed code cannot poison what the user or the orchestrator runs at Medium. Per-project separation keeps
    projects from sharing entries by accident; it is NOT a boundary against hostile code: every Low-labelled
    directory (all worktrees, all sandbox dirs) is writable by every sandboxed process (documented residual).
    `read_only_cwd` (in_place projects): no bytecode written into the user's folder (PYTHONDONTWRITEBYTECODE)
    and pytest's cache plugin off: its `.pytest_cache` creation uses `tempfile.mkdtemp` in the project folder,
    and CPython on Windows retries mkdtemp on PermissionError (it reads "access denied" as a name collision)
    practically forever - observed: a passing pytest run hung until the verification timeout."""
    out = {k: v for k, v in env.items() if k.upper() not in _REDIRECTED}
    t = str(tmp)
    out.update({
        "TEMP": t, "TMP": t, "TMPDIR": t,
        "npm_config_cache": str(cache / "npm"),
        "npm_config_update_notifier": "false",
        "UV_CACHE_DIR": str(cache / "uv"),
        "PIP_CACHE_DIR": str(cache / "pip"),
        "XDG_CACHE_HOME": str(cache / "xdg"),
    })
    if read_only_cwd:
        out["PYTHONDONTWRITEBYTECODE"] = "1"
        out["PYTEST_ADDOPTS"] = "-p no:cacheprovider"
    return out


_REDIRECTED = {"PYTEST_ADDOPTS", "TEMP", "TMP", "TMPDIR", "NPM_CONFIG_CACHE", "NPM_CONFIG_UPDATE_NOTIFIER",
               "UV_CACHE_DIR", "PIP_CACHE_DIR", "XDG_CACHE_HOME", "PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX"}


# --------------------------------------------------------------------------------- public API: processes


class SandboxedProcess:
    """A Low-integrity child process (Popen-like subset: pid, returncode, poll, wait, kill)."""

    def __init__(self, pid: int, handle: int):
        self.pid = pid
        self._h = handle
        self.returncode: Optional[int] = None

    def poll(self) -> Optional[int]:
        if self.returncode is None and self._h:
            if _k32.WaitForSingleObject(self._h, 0) == WAIT_OBJECT_0:
                self._collect()
        return self.returncode

    def wait(self, timeout: Optional[float] = None) -> int:
        if self.returncode is not None:
            return self.returncode
        ms = 0xFFFFFFFF if timeout is None else max(0, int(timeout * 1000))
        if _k32.WaitForSingleObject(self._h, ms) != WAIT_OBJECT_0:
            raise subprocess.TimeoutExpired("sandboxed process", timeout or 0)
        self._collect()
        assert self.returncode is not None
        return self.returncode

    def _collect(self) -> None:
        code = wt.DWORD()
        if not _k32.GetExitCodeProcess(self._h, ctypes.byref(code)):
            raise _winerr("GetExitCodeProcess")
        # Called only after the process handle signals: 259 is then a real exit code, not STILL_ACTIVE.
        self.returncode = ctypes.c_int32(code.value).value if code.value > 0x7FFFFFFF else code.value
        self.close()

    def kill(self) -> None:
        if self.returncode is None and self._h:
            _k32.TerminateProcess(self._h, 1)

    def close(self) -> None:
        if self._h:
            _k32.CloseHandle(self._h)
            self._h = 0

    def __del__(self) -> None:  # pragma: no cover - finaliser
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass


class CommandJob:
    """A per-command Job Object nested inside the worker job: at the end of a verification command every process
    it started is terminated (`close`), including grandchildren whose parent had already exited - a Low process
    left behind could otherwise keep writing the worktree while the orchestrator commits it."""

    def __init__(self) -> None:
        self._h = 0
        self._drained = False
        if not IS_WINDOWS:
            raise SandboxUnavailable("no sandbox implementation on this platform (Windows only)")
        h = _k32.CreateJobObjectW(None, None)
        if not h:
            raise SandboxUnavailable(f"cannot create the per-command job: {_winerr('CreateJobObjectW')}")
        self._h = h
        limits = wt.DWORD(UI_RESTRICTIONS)
        if not _k32.SetInformationJobObject(h, JobObjectBasicUIRestrictions, ctypes.byref(limits), 4):
            err = _winerr("SetInformationJobObject(UI restrictions)")
            self.close()
            raise SandboxUnavailable(f"cannot set the UI limits of the per-command job: {err}")

    def ui_restrictions(self) -> int:
        out, n = wt.DWORD(), wt.DWORD()
        if not _k32.QueryInformationJobObject(self._h, JobObjectBasicUIRestrictions, ctypes.byref(out), 4,
                                              ctypes.byref(n)):
            raise _winerr("QueryInformationJobObject")
        return out.value

    def assign(self, pid: int) -> None:
        h = _k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            raise SandboxUnavailable(f"cannot open process {pid}: {_winerr('OpenProcess')}")
        try:
            if not _k32.AssignProcessToJobObject(self._h, h):
                raise SandboxUnavailable(f"cannot assign process {pid} to the per-command job: "
                                         f"{_winerr('AssignProcessToJobObject')}")
        finally:
            _k32.CloseHandle(h)

    def terminate(self) -> None:
        if self._h:
            if not _k32.TerminateJobObject(self._h, 1):
                raise SandboxUnavailable(f"cannot terminate command job: {_winerr('TerminateJobObject')}")

    def close(self) -> None:
        if self._h:
            self.terminate()
            deadline = time.monotonic() + 10
            while True:
                # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION.ActiveProcesses.
                accounting = ctypes.create_string_buffer(48)
                if not _k32.QueryInformationJobObject(self._h, 1, accounting, len(accounting), None):
                    raise SandboxUnavailable(f"cannot verify job exit: {_winerr('QueryInformationJobObject')}")
                if wt.DWORD.from_buffer(accounting, 40).value == 0:
                    break
                if time.monotonic() >= deadline:
                    raise SandboxUnavailable("command job still contains live processes after termination")
                time.sleep(0.01)
            self._drained = True
            _k32.CloseHandle(self._h)
            self._h = 0


class SandboxDesktop:
    """A desktop of its own for one command (N2 re-review S4), on the interactive window station: the command
    sees no window, no foreground window and no pixels of the user's desktop. Clipboard and global atoms belong
    to the window station; the job's UI limits deny them. Only this user (and SYSTEM) may open it; Low label so
    the Low token can create windows on it. Closed after the command."""

    def __init__(self, appcontainer_sid: Optional[str] = None) -> None:
        self._h = 0
        if not IS_WINDOWS:
            raise SandboxUnavailable("no sandbox implementation on this platform (Windows only)")
        self.name_only = f"orch-sbx-{os.urandom(6).hex()}"
        try:
            user = current_user_sid()
        except OSError as exc:
            raise SandboxUnavailable(f"cannot read the user SID: {exc}") from exc
        psd = ctypes.c_void_p()
        sddl = f"D:P(A;;GA;;;{user})(A;;GA;;;SY)S:(ML;;NW;;;LW)"
        if appcontainer_sid:
            sddl = f"D:P(A;;GA;;;{user})(A;;GA;;;SY)(A;;GA;;;{appcontainer_sid})S:(ML;;NW;;;LW)"
        if not _adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(psd), None):
            raise SandboxUnavailable(f"cannot build the desktop DACL: {_winerr('ConvertStringSecurityDescriptor')}")
        try:
            sa = _SECURITY_ATTRIBUTES(ctypes.sizeof(_SECURITY_ATTRIBUTES), psd.value, False)
            h = _u32.CreateDesktopW(self.name_only, None, None, 0, GENERIC_ALL, ctypes.byref(sa))
            if not h:
                raise SandboxUnavailable(f"cannot create the sandbox desktop: {_winerr('CreateDesktopW')}")
            self._h = h
        finally:
            _k32.LocalFree(psd)
        self.name = f"{_window_station_name()}\\{self.name_only}"

    def close(self) -> None:
        if self._h:
            _u32.CloseDesktop(self._h)
            self._h = 0


def _window_station_name() -> str:
    h = _u32.GetProcessWindowStation()
    buf = ctypes.create_unicode_buffer(256)
    n = wt.DWORD()
    if not h or not _u32.GetUserObjectInformationW(h, 2, buf, ctypes.sizeof(buf), ctypes.byref(n)):  # UOI_NAME
        raise SandboxUnavailable(f"cannot read the window station name: {_winerr('GetUserObjectInformationW')}")
    return buf.value


def _env_block(env: dict[str, str]) -> str:
    items = sorted(env.items(), key=lambda kv: kv[0].upper())
    for k, v in items:
        if "=" in k[1:] or "\0" in k or "\0" in v or not k:
            raise ValueError(f"invalid environment entry {k!r}")
    return "".join(f"{k}={v}\0" for k, v in items) + "\0"


@timing.timed("spawn")
def spawn(argv: Sequence[str], *, cwd: str, env: dict[str, str], stdout_fd: int, extra_flags: int = 0,
          desktop: Optional[str] = None, profile=None) -> SandboxedProcess:
    """Start `argv` at Low integrity, suspended; the caller must confine it (job) and resume it.

    stdout and stderr go to `stdout_fd` (an open file descriptor), stdin is NUL. Raises SandboxUnavailable when
    the Low token cannot be created and OSError when the process cannot be started."""
    if not IS_WINDOWS:
        raise SandboxUnavailable("no sandbox implementation on this platform (Windows only); verification "
                                 "commands are not run")
    if profile is None:
        raise SandboxUnavailable("verification requires an AppContainer profile")
    try:
        token = _create_low_token()
    except OSError as exc:
        raise SandboxUnavailable(f"cannot create the Low-integrity token: {exc}") from exc
    nul = open(os.devnull, "rb")  # noqa: SIM115 - closed below
    handles: list[int] = []
    attr = None
    try:
        h_out = _inheritable_dup(msvcrt.get_osfhandle(stdout_fd))
        handles.append(h_out)
        h_in = _inheritable_dup(msvcrt.get_osfhandle(nul.fileno()))
        handles.append(h_in)
        size = ctypes.c_size_t(0)
        _k32.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
        attr = ctypes.create_string_buffer(size.value)
        if not _k32.InitializeProcThreadAttributeList(attr, 2, 0, ctypes.byref(size)):
            raise _winerr("InitializeProcThreadAttributeList")
        hlist = (wt.HANDLE * 2)(h_in, h_out)
        if not _k32.UpdateProcThreadAttribute(attr, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, hlist,
                                              ctypes.sizeof(hlist), None, None):
            raise _winerr("UpdateProcThreadAttribute")
        from orchestrator.security.appcontainer import SECURITY_CAPABILITIES_ATTRIBUTE

        capabilities = profile.capabilities()
        if not _k32.UpdateProcThreadAttribute(attr, 0, SECURITY_CAPABILITIES_ATTRIBUTE,
                                             ctypes.byref(capabilities), ctypes.sizeof(capabilities), None, None):
            raise _winerr("UpdateProcThreadAttribute(SecurityCapabilities)")
        si = _STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(si)
        si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        si.StartupInfo.hStdInput = h_in
        si.StartupInfo.hStdOutput = h_out
        si.StartupInfo.hStdError = h_out
        desk_buf = ctypes.create_unicode_buffer(desktop) if desktop else None
        if desk_buf is not None:
            si.StartupInfo.lpDesktop = ctypes.cast(desk_buf, wt.LPWSTR)
        si.lpAttributeList = ctypes.cast(attr, ctypes.c_void_p)
        pi = _PROCESS_INFORMATION()
        cmdline = ctypes.create_unicode_buffer(subprocess.list2cmdline(list(argv)))
        block = ctypes.create_unicode_buffer(_env_block(env))
        flags = (CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT
                 | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW | extra_flags)
        if not _adv.CreateProcessAsUserW(token, None, cmdline, None, None, True, flags, block, cwd,
                                         ctypes.byref(si), ctypes.byref(pi)):
            raise _winerr("CreateProcessAsUserW")
        _k32.CloseHandle(pi.hThread)
        return SandboxedProcess(pi.dwProcessId, pi.hProcess)
    finally:
        if attr is not None:
            _k32.DeleteProcThreadAttributeList(attr)
        for h in handles:
            _k32.CloseHandle(h)
        nul.close()
        _k32.CloseHandle(token)


_RETAINED_SESSIONS: set[Session] = set()


class Session:
    """Everything one sandboxed verification command needs, prepared before anything runs (fail closed).

    `cwd` (constructor argument, kept as `source`): the orchestrator task worktree. It is NEVER granted to the
    sandbox identity, relabelled or written by the command. With `writable_cwd` the session owns a disposable
    copy-in tree (`verify_tree.VerificationTree`): `self.cwd` is that copy - the command's working directory and
    the only place besides scratch it can write - and `self.root` is the per-run directory holding the copy, the
    per-command `tmp/<id>/` and `cache-<identity>/`. All of it is removed by `finish`; nothing is copied back
    and nothing survives into another command, so no later Medium actor or later run can meet an alias the
    command created. The AppContainer identity is derived from the random copy path: one fresh SID per command.
    `root` (constructor argument): the per-project sandbox directory (Medium), parent of `runs/<id>/`.
    Without `writable_cwd` (in_place project folder, any other directory) nothing is copied or granted and
    `prepare` refuses the command.
    `data_dir` / `protected_files`: the orchestrator data dir and the files that must carry its no-read-up label
    (database, token); a missing label refuses the command (SandboxUnavailable). None only for callers that run
    without an orchestrator data dir (the server always passes it).

    Worktree snapshot (writable cwd only): the `.git` link file must be a regular file before the command
    (SandboxTampered otherwise), and it and the agent instruction / configuration files (`agent_files`) are
    compared after it. Any difference is restored without following links and reported (`tampered`); the caller
    turns it into a `sandbox_tamper:` result that blocks the task."""

    def __init__(self, cwd: str, root: Path, *, writable_cwd: bool, data_dir: Optional[Path] = None,
                 protected_files: Sequence[Path] = (), start_guard: Optional[Callable[[], None]] = None):
        self.source = cwd
        self.cwd = cwd
        self.root = Path(root)
        self.writable_cwd = writable_cwd
        self._start_cancelled = threading.Event()
        self._start_guard = start_guard
        self._tree = None
        if writable_cwd:
            from orchestrator.security import verify_tree

            self._tree = verify_tree.VerificationTree(cwd, self.root, check=self._check_start_cancelled)
            self.cwd = str(self._tree.path)
            self.root = self._tree.base
        self.data_dir = Path(data_dir) if data_dir is not None else None
        self.protected_files = [Path(p) for p in protected_files]
        self.tmp = self.root / "tmp" / os.urandom(8).hex()
        from orchestrator.security import appcontainer

        self._profile = None
        self._identity = appcontainer.profile_name(cwd)
        self.cache = self.root / ("cache-" + self._identity)
        self._gitlink: Optional[bytes] = None
        self._agent_snapshot = None
        self._desktop: Optional[SandboxDesktop] = None
        self._command_job = None
        self._unverified_startup_process = None
        self._runtime_lease = None
        self._tmp_created = False
        self.tampered: list[str] = []
        # Cleanup that could not complete while the run directory stays provably contained (verify_tree
        # ResidueRetained): logged and retried by verify_tree.sweep, never a security finding.
        self.residue: list[str] = []
        self._finished = False
        self._started = False
        self._finish_lock = threading.RLock()  # finish may be retried by settle_retained_sessions()

    def cancel_pending_start(self) -> None:
        """Record a permanent veto without waiting for native resume.

        Passing the final cancellation check commits resume. If cancellation races after
        that check, the caller joins startup and cleans up the adopted late process/job.
        This does not interrupt trusted native preparation; finish must wait for that thread.
        """
        self._start_cancelled.set()

    def _check_start_cancelled(self) -> None:
        if self._start_cancelled.is_set():
            raise SandboxUnavailable("verification startup was cancelled before resume")
        if self._start_guard is not None:
            self._start_guard()

    def _check_data_dir(self) -> None:
        if self.data_dir is None:
            return
        problems = check_data_dir_label(self.data_dir)
        for f in self.protected_files:
            if not f.exists():
                continue
            try:
                label = read_label(f)
            except OSError as exc:
                problems.append(f"cannot read the integrity label of {f}: {exc}")
                continue
            if label is None or label[0] < MEDIUM_RID or label[1] & (NO_WRITE_UP | NO_READ_UP) != (NO_WRITE_UP | NO_READ_UP):
                problems.append(f"{f} is not covered by the data dir's no-read-up label")
        if problems:
            raise SandboxUnavailable("; ".join(problems))

    def _prepare_dirs(self) -> None:
        # The project sandbox root: Medium, a plain directory. A root left Low-labelled by an older version (whose
        # tmp/ a Low process could have replaced with a junction) is removed without following links and rebuilt.
        medium_plain_root(self.root)
        tmp_parent = real_dir(self.root / "tmp", create=True)
        if _low_label_ok(str(tmp_parent)):
            remove_tree(tmp_parent)
            real_dir(tmp_parent, create=True)
        real_dir(self.cache, create=True)
        from orchestrator.security import appcontainer

        sid = appcontainer.task_sid(self.cwd)
        appcontainer.grant_directory(self.cache, sid, writable=True)
        label_low(self.cache)  # once: a pinned directory keeps its label (relabelling needs WRITE_OWNER)
        pin_directory(self.cache)
        try:
            os.mkdir(self.tmp)
        except OSError as exc:
            raise SandboxUnavailable(f"cannot create the sandbox temp dir: {exc}") from exc
        self._tmp_created = True
        appcontainer.grant_directory(self.tmp, sid, writable=True, private_scratch=True)
        label_low(self.tmp)

    @timing.timed("prepare")
    def prepare(self) -> "Session":
        from orchestrator.security import agent_files

        if not IS_WINDOWS:
            raise SandboxUnavailable("no sandbox implementation on this platform (Windows only); verification "
                                     "commands are not run")
        self._check_data_dir()
        if not self.writable_cwd:
            raise SandboxUnavailable("AppContainer verification requires an Orchestrator-owned task worktree")
        from orchestrator.gitops import git
        from orchestrator.security import appcontainer, paths, verify_tree

        # Copy-in: the real worktree is validated and read through no-follow handles into a fresh
        # orchestrator-owned tree; every grant, label and pin below applies to that copy only. An alias
        # already present in the worktree is refused (SandboxTampered) before anything is created.
        if self._tree is None or self._tree.path != Path(self.cwd) or self._tree.base != self.root:
            raise SandboxUnavailable("verification requires its own copy-in tree")
        self._tree.create()
        self._check_start_cancelled()
        # Defence in depth on the copy itself: plain entries, single links, regular `.git` link file.
        try:
            verify_tree.preflight(self.cwd, check=self._check_start_cancelled)
            gitlink = paths.read_inside(self.cwd, ".git", max_bytes=65536)
        except (git.UnsafeWorktree, paths.PathPolicyError, OSError):
            raise SandboxTampered("unsafe worktree or Git metadata before sandbox preparation") from None
        self._check_start_cancelled()

        try:
            self._prepare_dirs()
        except (appcontainer.AppContainerUnavailable, OSError) as exc:
            raise SandboxUnavailable(str(exc)) from exc
        if self.writable_cwd:
            from orchestrator.security import appcontainer

            try:
                appcontainer.grant_directory(Path(self.cwd), appcontainer.task_sid(self.cwd), writable=True)
            except appcontainer.AppContainerUnavailable as exc:
                raise SandboxUnavailable(str(exc)) from exc
            label_low(self.cwd)
            pin_directory(self.cwd)
            link = os.path.join(self.cwd, ".git")
            st = _lstat_reparse(link)
            import stat as stat_mod

            if st is None or is_reparse(st) or not stat_mod.S_ISREG(st.st_mode):
                raise SandboxTampered("the worktree's .git link is missing or not a regular file "
                                      "(directory, junction or symlink); the task is blocked for review")
            self._gitlink = gitlink
            try:
                self._agent_snapshot = agent_files.snapshot(self.cwd)
            except OSError as exc:
                raise SandboxUnavailable(f"cannot snapshot the agent configuration files: {exc}") from exc
        return self

    def env(self, env: dict[str, str]) -> dict[str, str]:
        # Verification has no provider authentication needs. Do not inherit profile
        # locations, Python path hooks or arbitrary credential variables.
        allowed = {"systemroot", "windir", "comspec", "pathext", "number_of_processors",
                   "processor_architecture", "ai_orchestrator_depth", "path"}
        clean = {k: v for k, v in env.items() if k.casefold() in allowed}
        clean.update({k: str(self.tmp) for k in ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOME")})
        clean["HOMEDRIVE"], clean["HOMEPATH"] = os.path.splitdrive(str(self.tmp))
        clean["PYTHONNOUSERSITE"] = "1"
        clean["PYTHONDONTWRITEBYTECODE"] = "1"
        return sandbox_env(clean, tmp=self.tmp, cache=self.cache, read_only_cwd=not self.writable_cwd)

    @timing.timed("start")
    def start(self, argv: Sequence[str], *, env: dict[str, str], stdout_fd: int,
              confine: "Callable[[int], None]") -> tuple[SandboxedProcess, CommandJob]:
        """Spawn suspended at Low IL, `confine(pid)` (worker job, no resume), nest a per-command job, resume.
        Any failure kills the still-suspended process and raises (SandboxUnavailable / OSError)."""
        from orchestrator.security import appcontainer, node_runtime, python_runtime

        if self._started or self._finished:
            raise SandboxUnavailable("verification sessions are single-use")
        self._check_start_cancelled()
        self._started = True
        try:
            job = CommandJob()
            self._command_job = job
        except OSError as exc:
            raise SandboxUnavailable(f"cannot create command job: {exc}") from exc
        try:
            try:
                child_env = self.env(env)
                if node_runtime.command_kind(argv) is not None:
                    self._runtime_lease = node_runtime.NodeRuntimeLease()
                    argv, child_env = node_runtime.verification_command(argv, cwd=self.cwd, env=child_env,
                        tmp=self.tmp, cache=self.cache, lease=self._runtime_lease)
                else:
                    self._runtime_lease = python_runtime.RuntimeLease()
                    argv = python_runtime.verification_argv(argv, cwd=self.cwd, env=child_env,
                                                            lease=self._runtime_lease)
                self._check_start_cancelled()
                self._profile = appcontainer.Profile(self.cwd)
                self._profile.create()
            except (appcontainer.AppContainerUnavailable, python_runtime.RuntimeUnavailable, OSError) as exc:
                raise SandboxUnavailable(str(exc)) from exc
            if self._desktop is None:
                self._desktop = SandboxDesktop(self._profile.sid)
            self._check_start_cancelled()
            proc = spawn(argv, cwd=self.cwd, env=child_env, stdout_fd=stdout_fd, desktop=self._desktop.name,
                         profile=self._profile)
        except BaseException as exc:
            job.close()
            if isinstance(exc, (OSError, appcontainer.AppContainerUnavailable)):
                raise SandboxUnavailable(f"cannot establish AppContainer process: {exc}") from exc
            raise
        try:
            confine(proc.pid)
            job.assign(proc.pid)
            appcontainer.verify_process(proc.pid, self._profile.sid)
            if process_integrity_rid(proc.pid) != LOW_RID:  # never resume a process we could not verify
                raise SandboxUnavailable("the spawned process is not at Low integrity")
            self._check_start_cancelled()  # Resume commit; late cancellation joins and drains this process.
            if _resume(proc.pid) is False:
                raise OSError(f"could not resume sandboxed process {proc.pid}")
        except BaseException as exc:
            self._unverified_startup_process = proc
            exited = False
            try:
                proc.kill()
                proc.wait(10)
                exited = True
            finally:
                try:
                    job.close()
                finally:
                    if exited:
                        self._unverified_startup_process = None
                        proc.close()
            if isinstance(exc, (OSError, appcontainer.AppContainerUnavailable)):
                raise SandboxUnavailable(f"AppContainer confinement verification failed: {exc}") from exc
            raise
        return proc, job

    @timing.timed("finish")
    def finish(self) -> list[str]:
        """Close the desktop, remove the per-command temp dir (never following links), compare and restore the
        worktree's `.git` link and agent files. Returns the tampering findings. Call only after every process of
        the command has ended (the per-command job is closed)."""
        with self._finish_lock:
            return self._finish_locked()

    def _finish_locked(self) -> list[str]:
        from orchestrator.security import agent_files

        if self._finished:
            return self.tampered
        if self._unverified_startup_process is not None or (
                self._command_job is not None and not self._command_job._drained):
            if self not in _RETAINED_SESSIONS:
                self.tampered.append("finish deferred: startup process exit not verified"
                                     if self._unverified_startup_process is not None
                                     else "finish deferred: command job exit not verified")
                _RETAINED_SESSIONS.add(self)
            # No tree inspection/restoration, desktop/profile deletion or runtime
            # release while an untrusted writer may remain. Keep ownership alive
            # even if the caller abandons this session after quarantine.
            return self.tampered
        if self._desktop is not None:
            try:
                self._desktop.close()
            except Exception as exc:
                self.tampered.append(f"cannot close the sandbox desktop: {exc}")
        if self._profile is not None:
            try:
                if self._command_job is None or not self._command_job._drained:
                    raise SandboxUnavailable("profile retained because command job exit could not be verified")
                self._profile.close()
            except Exception as exc:
                self.tampered.append(f"cannot delete the owned AppContainer profile: {exc}")
        if self._tmp_created and self._tree is None:  # with a copy-in tree, scratch is removed with the run dir
            try:
                parent = _lstat_reparse(str(self.tmp.parent))
                if parent is None or is_reparse(parent):
                    raise OSError("sandbox temp parent is missing or a reparse point")
                remove_tree(self.tmp)
            except Exception as exc:
                self.tampered.append(f"cannot remove the sandbox temp directory: {exc}")
        if self._gitlink is not None:
            try:
                self._restore_gitlink()
            except Exception as exc:
                self.tampered.append(f"cannot re-check or restore the .git link: {exc}")
        if self._agent_snapshot is not None:
            try:
                after = agent_files.snapshot(self.cwd)
            except Exception as exc:
                self.tampered.append(f"cannot re-check the agent configuration files: {exc}")
            else:
                try:
                    changes = agent_files.diff(self._agent_snapshot, after)
                    if changes:
                        self.tampered.append("agent instruction/configuration files were changed by the command: "
                                             + ", ".join(changes[:20]))
                        log.error("SECURITY: sandboxed verification changed agent configuration in %s: %s", self.cwd,
                                  changes[:20])
                        failures = agent_files.restore(self.cwd, self._agent_snapshot, after)
                        self.tampered += [f"restore failed: {f}" for f in failures[:20]]
                except Exception as exc:
                    self.tampered.append(f"cannot compare or restore the agent configuration files: {exc}")
        if self._runtime_lease is not None:
            try:
                if self._command_job is not None and not self._command_job._drained:
                    raise SandboxUnavailable("runtime lease retained because job exit could not be verified")
                self._runtime_lease.close()
            except Exception as exc:
                self.tampered.append(f"cannot release runtime lease: {exc}")
        if self._tree is not None:
            from orchestrator.security import verify_tree

            try:
                # Copy, scratch and caches of this command; every process has ended (job drained above).
                self._tree.destroy()
            except verify_tree.ResidueRetained as exc:
                self.residue.append(str(exc))  # contained; retried by the sweep below and by later sweeps
            except Exception as exc:
                self.tampered.append(f"cannot remove the verification tree: {exc}")
            # Bounded retry of residue left by earlier commands of this project (never raises).
            self.residue += verify_tree.sweep(self._tree.sandbox_root)
        self._finished = True
        _RETAINED_SESSIONS.discard(self)
        return self.tampered

    def _settle(self) -> bool:
        """Retry the exit verification this session was retained for, then finish. True when it finished.
        Nothing is released on any doubt: the job must report zero active processes after termination and the
        startup process handle must have signalled; otherwise the session stays retained exactly as before."""
        with self._finish_lock:
            if self._finished:
                return True
            try:
                job = self._command_job
                if job is not None and not job._drained:
                    job.close()  # terminate + verify no active process; raises while the exit is unverified
                proc = self._unverified_startup_process
                if proc is not None:
                    if proc.poll() is None:
                        return False
                    proc.close()
                    self._unverified_startup_process = None
            except Exception as exc:  # noqa: BLE001 - still unverified: keep everything retained
                log.warning("SECURITY: retained verification session still unverified: %s", exc)
                return False
            findings = list(self._finish_locked())
            if not self._finished:
                return False
            log.warning("retained verification session settled after its job exit was verified: %s",
                        "; ".join(findings) or "no findings")
            return True

    def _restore_gitlink(self) -> None:
        import stat as stat_mod

        link = os.path.join(self.cwd, ".git")
        st = _lstat_reparse(link)
        current: Optional[bytes] = None
        if st is not None and not is_reparse(st) and stat_mod.S_ISREG(st.st_mode):
            try:
                with open(link, "rb") as fh:
                    current = fh.read(65536)
            except OSError:
                current = None
        if current == self._gitlink:
            return
        self.tampered.append("the worktree's .git link was changed by the command and has been restored")
        log.error("SECURITY: sandboxed verification changed %s; restoring it", link)
        try:
            if st is not None:
                if is_reparse(st):
                    _unlink_entry(link, st)  # a junction/symlink is removed as a link, never followed
                elif _is_dir_stat(st):
                    remove_tree(link)
                else:
                    _unlink_entry(link, st)
            if os.path.lexists(link):
                raise OSError("the old .git entry could not be removed")
            fd = os.open(link, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644)
            with os.fdopen(fd, "wb") as fh:
                fh.write(self._gitlink or b"")
        except OSError as exc:
            self.tampered.append(f"restoring the .git link failed: {exc}")


def prewarm_verification_runtimes() -> None:
    """Stage (first run only), pin and fully validate the managed Python and Node runtimes once, in the
    background at server start, through the same shared leases sessions use. The first verification command
    then reuses the validated pins instead of paying the cold pin/hash/ACL pass (observed up to ~1 min under
    I/O load or on-access scanning) inside its own unchanged deadline. Pins stay until shutdown. Failures
    are only logged: a real verification still validates and fails closed on its own."""
    from orchestrator.security import node_runtime, python_runtime

    for name, make in (("python", python_runtime.RuntimeLease), ("node", node_runtime.NodeRuntimeLease)):
        try:
            lease = make()
            try:
                lease.acquire()
            finally:
                lease.close()
            log.info("verification runtime prewarmed: %s", name)
        except Exception as exc:  # noqa: BLE001 - best effort; verification re-validates and fails closed
            log.warning("verification runtime prewarm skipped (%s): %s", name, exc)


def retained_sessions() -> int:
    """How many sessions are retained because a job or startup process exit could not be verified."""
    return len(_RETAINED_SESSIONS)


_SETTLE_LOCK = threading.Lock()


def settle_retained_sessions() -> int:
    """Retry the deferred finish of every retained session; returns how many remain retained.

    A session is retained (runtime reference, AppContainer profile, desktop and run directory all kept) when
    the exit of its command job or startup process could not be verified. That state is fail closed, not
    final: once the job can be terminated and reports no active process, the normal finish runs (inspection,
    runtime release, run directory destroyed or moved to residue). Called before verification commands and at
    server shutdown. Never raises; an unverifiable session is left untouched."""
    with _SETTLE_LOCK:
        for session in list(_RETAINED_SESSIONS):
            try:
                session._settle()
            except Exception:  # noqa: BLE001 - one session must not block the others
                log.exception("cannot settle a retained verification session")
        return len(_RETAINED_SESSIONS)


@timing.timed("resume")
def _resume(pid: int) -> bool:
    import psutil

    try:
        psutil.Process(pid).resume()
        return True
    except psutil.Error:
        return False


def process_integrity_rid(pid: int) -> int:
    """Integrity RID of a running process (tests, doctor). Raises OSError."""
    if not IS_WINDOWS:
        raise OSError("integrity levels exist only on Windows")
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        raise _winerr("OpenProcess")
    try:
        tok = wt.HANDLE()
        if not _adv.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(tok)):
            raise _winerr("OpenProcessToken")
        try:
            return _token_integrity_rid(tok.value)
        finally:
            _k32.CloseHandle(tok)
    finally:
        _k32.CloseHandle(h)


def self_test() -> Optional[str]:
    """None when a Low token can be created here, otherwise the reason (doctor)."""
    if not IS_WINDOWS:
        return "no sandbox implementation on this platform (Windows only)"
    try:
        _k32.CloseHandle(_create_low_token())
    except OSError as exc:
        return str(exc)
    return None


__all__ = [
    "ERROR_CODE", "LOW_RID", "MEDIUM_RID", "SECURITY_CODES", "TAMPER_CODE", "SandboxDesktop", "SandboxTampered",
    "SandboxUnavailable", "SandboxedProcess", "check_data_dir_label", "is_elevated", "is_pinned", "is_security_error",
    "label_low", "pin_directory", "process_integrity_rid", "protect_data_dir", "read_label", "real_dir",
    "remove_tree", "sandbox_env", "self_test", "spawn", "supported", "token_groups_of_sandbox_token",
]
