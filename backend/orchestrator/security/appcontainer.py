"""Windows verification identities. No capabilities, exemptions, or Low-only fallback.

Profile names are scoped to this installation and a canonical task worktree. An
already-existing profile is refused: it is never adopted or deleted by guessing
ownership. Only profiles successfully created by this instance may be deleted.
"""

from __future__ import annotations

from orchestrator.security import timing

import ctypes
import hashlib
import os
from ctypes import wintypes as wt
from pathlib import Path


class AppContainerUnavailable(RuntimeError):
    pass


SECURITY_CAPABILITIES_ATTRIBUTE = 0x20009


class SecurityCapabilities(ctypes.Structure):
    _fields_ = [("AppContainerSid", ctypes.c_void_p), ("Capabilities", ctypes.c_void_p),
                ("CapabilityCount", wt.DWORD), ("Reserved", wt.DWORD)]


def profile_name(worktree: str | Path) -> str:
    install = os.path.normcase(str(Path(__file__).resolve().parents[3]))
    task = os.path.normcase(os.path.realpath(worktree))
    return "ai-orchestrator-v1-" + hashlib.sha256((install + "\0" + task).encode()).hexdigest()[:40]


def _api():
    if os.name != "nt":
        raise AppContainerUnavailable("AppContainer requires Windows")
    api = ctypes.WinDLL("userenv", use_last_error=True)
    api.DeriveAppContainerSidFromAppContainerName.argtypes = [wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    api.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
    api.CreateAppContainerProfile.argtypes = [wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, ctypes.c_void_p,
                                             wt.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    api.CreateAppContainerProfile.restype = ctypes.c_long
    api.DeleteAppContainerProfile.argtypes = [wt.LPCWSTR]
    api.DeleteAppContainerProfile.restype = ctypes.c_long
    return api


def _free_sid(sid):
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.FreeSid.argtypes = [ctypes.c_void_p]
    adv.FreeSid.restype = ctypes.c_void_p
    adv.FreeSid(sid)


def _check(hr: int, operation: str):
    if hr != 0:
        raise AppContainerUnavailable(f"{operation} failed (HRESULT 0x{hr & 0xffffffff:08x})")


def task_sid(worktree: str | Path) -> str:
    from orchestrator.security import sandbox

    sid = ctypes.c_void_p()
    _check(_api().DeriveAppContainerSidFromAppContainerName(profile_name(worktree), ctypes.byref(sid)),
           "DeriveAppContainerSidFromAppContainerName")
    try:
        return sandbox._sid_str(sid.value)
    finally:
        _free_sid(sid)


def loopback_exemptions() -> set[str]:
    """Read Windows' loopback allowlist; never add or remove an exemption.

    Memory ownership follows Microsoft's NetworkIsolationGetAppContainerConfig
    example: each SID and the array are allocated from the process heap.
    """
    from orchestrator.security import sandbox as s

    api = ctypes.WinDLL("FirewallAPI.dll", use_last_error=True)
    entries = ctypes.POINTER(s._SID_AND_ATTRIBUTES)()
    count = wt.DWORD()
    query = api.NetworkIsolationGetAppContainerConfig
    query.argtypes = [ctypes.POINTER(wt.DWORD), ctypes.POINTER(ctypes.POINTER(s._SID_AND_ATTRIBUTES))]
    query.restype = wt.DWORD
    error = query(ctypes.byref(count), ctypes.byref(entries))
    if error:
        raise AppContainerUnavailable(f"cannot inspect loopback exemptions (error {error})")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetProcessHeap.restype = wt.HANDLE
    kernel.HeapFree.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p]
    kernel.HeapFree.restype = wt.BOOL
    heap = kernel.GetProcessHeap()
    try:
        return {s._sid_str(entries[i].Sid) for i in range(count.value)}
    finally:
        for i in range(count.value):
            kernel.HeapFree(heap, 0, entries[i].Sid)
        if entries:
            kernel.HeapFree(heap, 0, entries)


def refuse_loopback_exemption(sid: str) -> None:
    if sid in loopback_exemptions():
        raise AppContainerUnavailable("task AppContainer has a loopback exemption; refusing to launch")


class Profile:
    def __init__(self, worktree: str | Path):
        self.name = profile_name(worktree)
        self.sid = task_sid(worktree)
        self._pointer = ctypes.c_void_p()
        self._owned = False

    @timing.timed("profile")
    def create(self):
        from orchestrator.security import sandbox

        if self._owned:
            raise AppContainerUnavailable("profile already active")
        _check(_api().CreateAppContainerProfile(self.name, self.name, "Orchestrator verification only",
                                               None, 0, ctypes.byref(self._pointer)),
               "CreateAppContainerProfile")
        self._owned = True
        if sandbox._sid_str(self._pointer.value) != self.sid:
            self.close()
            raise AppContainerUnavailable("created profile SID differs from expected task SID")
        refuse_loopback_exemption(self.sid)
        return self

    def capabilities(self):
        if not self._owned:
            raise AppContainerUnavailable("profile is not active")
        return SecurityCapabilities(self._pointer.value, None, 0, 0)

    def close(self):
        """Caller must first terminate and drain every process using this profile."""
        if self._owned:
            _check(_api().DeleteAppContainerProfile(self.name), "DeleteAppContainerProfile")
            self._owned = False
            _free_sid(self._pointer)
            self._pointer = ctypes.c_void_p()


def verify_process(pid: int, expected_sid: str) -> None:
    """Read the suspended child's actual token, before the caller resumes it."""
    from orchestrator.security import sandbox as s

    handle = s._k32.OpenProcess(0x1000, False, pid)
    if not handle:
        raise AppContainerUnavailable("cannot open child for AppContainer verification")
    token = wt.HANDLE()
    try:
        if not s._adv.OpenProcessToken(handle, 8, ctypes.byref(token)):
            raise AppContainerUnavailable("cannot query child AppContainer token")
        try:
            flag = s._token_info(token.value, 29)  # TokenIsAppContainer
            sid = s._token_info(token.value, 31)  # TokenAppContainerSid
            if ctypes.c_uint32.from_buffer(flag).value != 1:
                raise AppContainerUnavailable("child is not an AppContainer")
            pointer = ctypes.c_void_p.from_buffer(sid).value
            if not pointer or s._sid_str(pointer) != expected_sid:
                raise AppContainerUnavailable("child AppContainer SID mismatch")
            capabilities = s._token_info(token.value, 30)  # TokenCapabilities / TOKEN_GROUPS
            if ctypes.c_uint32.from_buffer(capabilities).value != 0:
                raise AppContainerUnavailable("child AppContainer has unexpected capabilities")
            refuse_loopback_exemption(expected_sid)
        finally:
            s._k32.CloseHandle(token)
    finally:
        s._k32.CloseHandle(handle)


def grant_directory(path: Path, sid: str, *, writable: bool, private_scratch: bool = False) -> None:
    """Grant on an Orchestrator-owned directory before pinning; never unpin it.

    The caller establishes ownership by construction. Existing pinned roots must
    already carry the exact grant; legacy worktrees fail closed and need recreation.
    Existing descendants with protected ACLs do not receive access implicitly.
    """
    from orchestrator.security import paths, sandbox as s

    if not sid.startswith("S-1-15-2-"):
        raise AppContainerUnavailable("not an AppContainer SID")
    if private_scratch and (not writable or sid in {"S-1-15-2-1", "S-1-15-2-2"}):
        raise AppContainerUnavailable("private scratch requires an exact writable task identity")
    # Per-command scratch: Modify + DELETE_CHILD (0x1301ff). Deliberately WITHOUT WRITE_DAC and WRITE_OWNER: a
    # command must not be able to re-ACL inherited scratch objects (e.g. an OWNER RIGHTS deny that defeats the
    # orchestrator's owner reset and leaves permanent residue). The Python 0700 bootstrap does not need them: it
    # creates its directory with an explicit descriptor, which requires only FILE_ADD_SUBDIRECTORY on the parent
    # (verified natively: test_appcontainer_final_review 0700 + real pytest tmp_path tests).
    # Worktree/cache roots retain Modify only.
    rights = "0x1301ff" if private_scratch else ("0x1301bf" if writable else "0x1200a9")
    ace = f"(A;OICI;{rights};;;{sid})"
    def exact_grant():
        from orchestrator.security import acl

        data = acl.read_security(path)
        mine = [a for a in data["aces"] or [] if a["sid"] == sid]
        # A narrow ACE must not conceal another broader grant for this identity.
        return (len(mine) == 1 and mine[0]["type"] == 0
                and mine[0]["mask"] == int(rights, 16) and mine[0]["flags"] & 3 == 3)
    with paths.directory_guard(path):
        h = s._k32.CreateFileW(str(path), 0x20000 | 0x40000, 7, None, 3,
                              0x02000000 | 0x00200000, None)
        if not h or h == s.INVALID_HANDLE_VALUE:
            # A pin deliberately denies WRITE_DAC. Inspect without weakening it.
            if exact_grant():
                return
            raise AppContainerUnavailable(f"cannot prepare AppContainer permissions on {path}; pinned legacy root")
        try:
            info = s._BY_HANDLE_FILE_INFORMATION()
            if not s._k32.GetFileInformationByHandle(h, ctypes.byref(info)) or info.dwFileAttributes & 0x400:
                raise AppContainerUnavailable("ACL target is not a plain directory")
            old = s._handle_dacl_sddl(h)
            if sid in old:
                if exact_grant():
                    return
                raise AppContainerUnavailable("existing AppContainer ACL is not the required exact grant")
            psd = ctypes.c_void_p()
            if not s._adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(old + ace, 1,
                                                                               ctypes.byref(psd), None):
                raise s._winerr("AppContainer DACL conversion")
            try:
                if not s._adv.SetKernelObjectSecurity(h, 4, psd):
                    raise s._winerr("AppContainer DACL grant")
                if not exact_grant():
                    raise AppContainerUnavailable("AppContainer ACL readback failed")
            finally:
                s._k32.LocalFree(psd)
        finally:
            s._k32.CloseHandle(h)
