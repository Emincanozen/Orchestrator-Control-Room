"""Bounded owned Codex homes; no adapter, credential discovery or process launch.

The caller supplies a sanitized environment and a subscription auth snapshot, joins
prepare even when cancelled, and calls close only AFTER verified provider/job drain.
close(retain=True) preserves every lease for uncertain exit; a later close() requires
verified drain. Strong references intentionally survive abandoned callers. There is
no destructor, automatic stale-auth recovery, retry or credential refresh support.

The permission rendering is a candidate for native exec/resume attack testing, NOT
proof of MXC/helper feasibility or production confinement. Helper/profile aliases
remain read-only; only the command scratch alias is writable. In particular the
adapter must still validate effective MCP inventory, MXC and selected permissions.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import re
import sys
import threading
from contextlib import ExitStack
from pathlib import Path, PureWindowsPath
from typing import ClassVar

from orchestrator.security import acl, paths
from orchestrator.security.codex_auth import MAX_AUTH_BYTES, CodexAuthUnavailable, validate_auth_snapshot

AUTH_LIMIT = MAX_AUTH_BYTES
_HELD: set[CodexRunContext] = set()
_HELD_LOCK = threading.Lock()
_ENV_ALLOWED = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "OS",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "LANG", "LC_ALL", "AI_ORCHESTRATOR_DEPTH",
})
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\.|$)", re.IGNORECASE)


class CodexRuntimeUnavailable(RuntimeError):
    """Fixed, non-secret diagnostics only; never chain an underlying exception."""


class CodexRuntimeError(CodexRuntimeUnavailable):
    """Internal refusal category; callers may catch CodexRuntimeUnavailable."""


class CodexRuntimeCancelled(CodexRuntimeError):
    pass


def _win_path(raw: str) -> str:
    if not isinstance(raw, str) or not raw or len(raw) > 4096:
        raise CodexRuntimeError("invalid runtime path")
    p = PureWindowsPath(raw)
    if (not p.is_absolute() or len(p.drive) != 2 or p.drive[1] != ":"
            or raw.startswith(("\\\\", "//"))):
        raise CodexRuntimeError("local absolute Windows paths required")
    # Reject aliases before normpath can conceal them. No ADS, short names or devices.
    components = raw.replace("/", "\\").split("\\")[1:]
    for part in components:
        if (part in {".", ".."} or part.endswith((".", " ")) or _RESERVED.match(part)
                or any(ord(c) < 32 or c in ':<>"|?*~' for c in part)):
            raise CodexRuntimeError("ambiguous runtime path")
    if not 2 <= len(p.parts) <= 64:
        raise CodexRuntimeError("drive roots or excessive path depth are unsupported")
    return ntpath.normcase(ntpath.normpath(raw))


def _within(a: str, b: str) -> bool:
    return a == b or a.startswith(b.rstrip("\\") + "\\")


def _overlaps(a: str, b: str) -> bool:
    return _within(a, b) or _within(b, a)


def _auth_identity(auth_bytes: bytes) -> str:
    failed = False
    try:
        return validate_auth_snapshot(auth_bytes)
    except CodexAuthUnavailable:
        failed = True
    finally:
        auth_bytes = b""
    if failed:
        raise CodexRuntimeError("unsupported subscription authentication snapshot")


def _home_key(cwd: str, extra_dirs: tuple[str, ...], identity: str) -> str:
    material = json.dumps(["codex-owned-v1", cwd, list(extra_dirs), identity],
                          ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(material).hexdigest()


def _q(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _render_config(cwd: str, extras: tuple[str, ...], scratch: str, aliases: tuple[str, ...]) -> bytes:
    """Deterministic TOML only. Installed CLI/native feasibility is separately unproven."""
    lines = [
        'model_provider = "openai"', 'cli_auth_credentials_store = "file"', 'approval_policy = "never"',
        'web_search = "disabled"', 'default_permissions = "orch-read"',
        '[windows]', 'sandbox = "mxc"', '[features]',
        'hooks = false', 'plugins = false', 'apps = false', 'multi_agent = false', '[mcp_servers]',
    ]
    projects = set()
    for root in (cwd, *extras):
        p = PureWindowsPath(root)
        projects.update(str(parent) for parent in (p, *p.parents))
    for project in sorted(projects):
        lines += [f'[projects.{_q(project)}]', 'trust_level = "untrusted"']
    for name, mode in (("orch-read", "read"), ("orch-write", "write")):
        lines += [f'[profiles.{name}]', f'default_permissions = "{name}"',
                  'approval_policy = "never"', 'web_search = "disabled"',
                  f'[permissions.{name}.network]', 'enabled = false',
                  f'[permissions.{name}.filesystem]', '":minimal" = "read"',
                  f'{_q(scratch)} = "write"']
        lines += [f'{_q(alias)} = "read"' for alias in aliases]
        scopes = ('":workspace_roots"', *(_q(root) for root in extras))
        for scope in scopes:
            lines += [f'[permissions.{name}.filesystem.{scope}]', f'"." = "{mode}"']
            lines += [f'"{protected}" = "read"' for protected in (".git", ".codex", ".agents", ".aws")]
    return ("\n".join(lines) + "\n").encode("ascii")


def _install_roots() -> tuple[str, ...]:
    from orchestrator.config import install_dirs

    roots = list(install_dirs())
    # Nested developer worktrees may themselves be under the source checkout.
    # Inspect markers only; never read git/user configuration or credential files.
    roots += [p for p in Path(__file__).absolute().parents if os.path.lexists(p / ".git")]
    return tuple(_win_path(str(p)) for p in roots)


def _assert_plain_roots(roots: tuple[str, ...]) -> None:
    # Caller must exclude other task writers, as for Medium Git access. A native
    # scoped profile alone does not protect a pre-planted hardlink's outside inode.
    from orchestrator.gitops.git import assert_plain_worktree

    for root in roots:
        assert_plain_worktree(root, include_git=True)


class _NativeFile:
    """Integer handles intentionally have no destructor. All failures retain ownership."""

    def __init__(self, io, handle, identity):
        self.io, self.handle, self.identity = io, handle, identity

    def read(self, limit: int) -> bytes:
        c, w, k = self.io.c, self.io.w, self.io.k
        data = c.create_string_buffer(limit + 1)
        count = w.DWORD()
        if not k.ReadFile(self.handle, data, len(data), c.byref(count), None):
            raise CodexRuntimeError("owned file read failed")
        if count.value > limit:
            raise CodexRuntimeError("owned file exceeds expected bound")
        return data.raw[:count.value]

    def write(self, data: bytes) -> None:
        try:
            c, w, k = self.io.c, self.io.w, self.io.k
            count = w.DWORD()
            if (not k.WriteFile(self.handle, data, len(data), c.byref(count), None)
                    or count.value != len(data) or not k.FlushFileBuffers(self.handle)):
                raise CodexRuntimeError("owned file write failed")
        finally:
            data = b""

    def close(self) -> None:
        if self.handle is not None:
            if not self.io.k.CloseHandle(self.handle):
                raise CodexRuntimeError("owned file lease release failed")
            self.handle = None
            self.io.leases.discard(self)


class _WindowsIO:
    """Native no-follow operations, with no InstanceLock, ACL repair or subprocess."""

    def __init__(self):
        if sys.platform != "win32":
            raise CodexRuntimeError("owned Codex runtime requires Windows")
        import ctypes as c
        from ctypes import wintypes as w

        self.c, self.w = c, w
        self.leases = set()
        self.k = c.WinDLL("kernel32", use_last_error=True)
        self.adv = c.WinDLL("advapi32", use_last_error=True)
        signatures = {
            "CreateFileW": ([w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE], w.HANDLE),
            "CreateDirectoryW": ([w.LPCWSTR, c.c_void_p], w.BOOL),
            "CloseHandle": ([w.HANDLE], w.BOOL),
            "ReadFile": ([w.HANDLE, c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.c_void_p], w.BOOL),
            "WriteFile": ([w.HANDLE, c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.c_void_p], w.BOOL),
            "FlushFileBuffers": ([w.HANDLE], w.BOOL),
            "GetFileInformationByHandle": ([w.HANDLE, c.c_void_p], w.BOOL),
            "GetFinalPathNameByHandleW": ([w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD], w.DWORD),
            "SetFileInformationByHandle": ([w.HANDLE, c.c_int, c.c_void_p, w.DWORD], w.BOOL),
            "LocalFree": ([c.c_void_p], c.c_void_p),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(self.k, name)
            fn.argtypes, fn.restype = args, result
        self.adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            w.LPCWSTR, w.DWORD, c.POINTER(c.c_void_p), c.c_void_p,
        ]
        self.adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = w.BOOL
        self.user_sid = acl.current_user_sid()

    def guard(self, path: Path):
        return paths.directory_guard(path)

    def private(self, path: Path) -> None:
        if acl.check_private(path):
            raise CodexRuntimeError("existing runtime path is not private; repair is forbidden")

    def _security(self, directory: bool):
        c, w = self.c, self.w

        class SA(c.Structure):
            _fields_: ClassVar = [
                ("nLength", w.DWORD), ("lpSecurityDescriptor", c.c_void_p), ("bInheritHandle", w.BOOL),
            ]

        inherit = "OICI" if directory else ""
        sddl = "D:P" + "".join(f"(A;{inherit};FA;;;{sid})" for sid in (self.user_sid, "SY", "BA"))
        sd = c.c_void_p()
        if not self.adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, c.byref(sd), None):
            raise CodexRuntimeError("owned object security descriptor unavailable")
        return SA(c.sizeof(SA), sd, False), sd

    def directory(self, path: Path, *, exclusive: bool = False) -> None:
        # Parent is already guarded by the caller. Protected DACL exists at creation,
        # so there is no mkdir-to-icacls path substitution window.
        sa, sd = self._security(True)
        try:
            if (not self.k.CreateDirectoryW(str(path), self.c.byref(sa))
                    and (exclusive or self.c.get_last_error() != 183)):
                raise CodexRuntimeError("owned directory creation refused")
        finally:
            self.k.LocalFree(sd)

    def _open(self, path: Path, access: int, share: int, disposition: int) -> _NativeFile:
        sa, sd = self._security(False)
        try:
            # OPEN_ALWAYS does not write/truncate. Lock is share=0. CREATE_NEW is
            # mandatory for config/auth creation. Handles are never inherited.
            h = self.k.CreateFileW(str(path), access, share, self.c.byref(sa), disposition,
                                   0x00200000 | 0x02000000, None)
        finally:
            self.k.LocalFree(sd)
        if not h or h == self.c.c_void_p(-1).value:
            raise CodexRuntimeError("owned file open refused")
        lease = _NativeFile(self, h, None)
        self.leases.add(lease)
        try:
            info = paths._BHFI()
            if not self.k.GetFileInformationByHandle(h, self.c.byref(info)):
                raise CodexRuntimeError("owned file inspection failed")
            if info.dwFileAttributes & (0x400 | 0x10) or info.nNumberOfLinks != 1:
                raise CodexRuntimeError("owned file must be regular and singly linked")
            buf = self.c.create_unicode_buffer(32768)
            count = self.k.GetFinalPathNameByHandleW(h, buf, len(buf), 0)
            if not count or count >= len(buf) or _win_path(buf.value.removeprefix("\\\\?\\")) != _win_path(str(path)):
                raise CodexRuntimeError("owned file final path mismatch")
            lease.identity = (info.dwVolumeSerialNumber, info.nFileIndexHigh, info.nFileIndexLow)
            self.private(path)
            return lease
        except BaseException:
            lease.close()
            raise

    def lock(self, path: Path) -> _NativeFile:
        return self._open(path, 0x80000000, 0, 4)  # READ, share0, OPEN_ALWAYS; no writes

    def create(self, path: Path) -> _NativeFile:
        return self._open(path, 0xC0000000, 1, 1)  # READ|WRITE, shareREAD, CREATE_NEW

    def pin(self, path: Path) -> _NativeFile:
        return self._open(path, 0x80000000, 1, 3)  # READ, shareREAD only, OPEN_EXISTING

    def exists(self, path: Path) -> bool:
        return os.path.lexists(path)

    def unsettled(self, known=()) -> bool:
        return bool(self.leases.difference(known))

    def remove_owned(self, path: Path, identity) -> None:
        # Reopen with DELETE/share0 after releasing the READ pin. Compare identity
        # before marking the OPENED object for deletion; never unlink a path blindly.
        lease = self._open(path, 0x80010000, 0, 3)
        try:
            if lease.identity != identity:
                raise CodexRuntimeError("owned auth identity changed; cleanup blocked")
            flag = self.c.c_ubyte(True)  # FILE_DISPOSITION_INFO.DeleteFile is BOOLEAN
            if not self.k.SetFileInformationByHandle(lease.handle, 4, self.c.byref(flag), self.c.sizeof(flag)):
                raise CodexRuntimeError("owned auth deletion refused")
        finally:
            lease.close()


class CodexRunContext:
    """Single-use, synchronous preparation. See module lifecycle/drain requirements.

    Existing data_dir must already be protected and outside install/task/TEMP
    trees. No existing ACL is repaired. close(retain=True) deliberately keeps auth
    and handles until verified drain; it is not a settled cleanup operation.
    """

    def __init__(self, data_dir: Path, cwd: str, extra_dirs: tuple[str, ...], child_env: dict[str, str]):
        if (type(extra_dirs) is not tuple or len(extra_dirs) > 32
                or type(child_env) is not dict or len(child_env) > 128):
            raise CodexRuntimeError("invalid runtime inputs")
        self._data = _win_path(str(data_dir))
        self._cwd = _win_path(cwd)
        self._extras = tuple(sorted({_win_path(p) for p in extra_dirs} - {self._cwd}))
        self._env_input = {}
        self._temp_roots = []
        seen = set()
        for key, value in child_env.items():
            if (type(key) is not str or type(value) is not str or len(key) > 128 or len(value) > 32767
                    or not key or "=" in key or any(ord(c) < 32 for c in key + value)):
                raise CodexRuntimeError("invalid sanitized child environment")
            key = key.upper()
            if key in seen:
                raise CodexRuntimeError("ambiguous child environment")
            seen.add(key)
            if key in {"TEMP", "TMP"}:
                self._temp_roots.append(_win_path(value))
            if key in _ENV_ALLOWED:
                self._env_input[key] = value
        self._veto = threading.Event()
        self._mutex = threading.RLock()
        self._state = "new"
        self._io = None
        self._guards = ExitStack()
        self._lock = self._config = self._auth = None
        self._auth_identity = None
        self._home = None
        self._environment = {}

    def __repr__(self) -> str:
        return f"<CodexRunContext state={self._state}>"

    def _check_cancel(self) -> None:
        if self._veto.is_set():
            raise CodexRuntimeCancelled("owned Codex preparation cancelled")

    def cancel_pending_start(self) -> None:
        """Nonblocking permanent veto; never wait for preparation's mutex here."""
        self._veto.set()

    @property
    def home(self) -> Path:
        if self._home is None:
            raise CodexRuntimeError("runtime home is not prepared")
        return self._home

    @property
    def config_path(self) -> Path:
        return self.home / "config.toml"

    @property
    def environment(self) -> dict[str, str]:
        with self._mutex:
            self._check_cancel()
            if self._state != "ready":
                raise CodexRuntimeError("runtime is not ready")
            return dict(self._environment)

    def _guard(self, path: Path) -> None:
        self._check_cancel()
        self._guards.enter_context(self._io.guard(path))
        self._check_cancel()

    def validate_before_launch(self) -> None:
        """Recheck task metadata immediately before each helper/provider launch.

        This detects aliases planted after preparation, not concurrent mutation
        after this check. The caller must still exclude other workspace writers
        and prove native containment before activating production execution.
        A failed check permanently vetoes this context; retain leases until any
        previously started process has verifiably drained.
        """
        with self._mutex:
            self._check_cancel()
            if self._state != "ready":
                raise CodexRuntimeError("runtime is not ready")
            try:
                _assert_plain_roots((self._cwd, *self._extras))
                self._check_cancel()
            except BaseException:
                self.cancel_pending_start()
                raise

    def _directory(self, path: Path, *, exclusive: bool = False) -> None:
        self._check_cancel()
        self._io.directory(path, exclusive=exclusive)
        self._guard(path)
        self._io.private(path)

    def _layout(self) -> None:
        roots = (self._cwd, *self._extras)
        installs = _install_roots()
        if any(_overlaps(self._data, root) for root in (*roots, *installs, *self._temp_roots)):
            raise CodexRuntimeError("runtime data overlaps a protected or task tree")
        if any(_overlaps(self._cwd, root) for root in installs):
            raise CodexRuntimeError("task root overlaps the installation")
        if any(part.lower() in {"temp", "tmp"} for part in PureWindowsPath(self._data).parts):
            raise CodexRuntimeError("runtime data in TEMP is unsupported")
        # An extra ancestor of cwd could widen :workspace_roots to the source tree.
        for extra in self._extras:
            if _overlaps(extra, self._cwd) or any(_overlaps(extra, p) for p in installs):
                raise CodexRuntimeError("overlapping additional roots are unsupported")

    def _create_and_pin(self, path: Path, content: bytes, *, auth: bool):
        try:
            writer = self._io.create(path)
            if auth:
                self._auth, self._auth_identity = writer, writer.identity
            else:
                self._config = writer
            identity = writer.identity
            self._check_cancel()
            writer.write(content)
            self._check_cancel()
            writer.close()
            pin = self._io.pin(path)
            if auth:
                self._auth = pin
            else:
                self._config = pin
            if pin.identity != identity or pin.read(len(content)) != content:
                raise CodexRuntimeError("new owned file changed before pin")
            self._check_cancel()
        finally:
            content = b""

    def prepare(self, auth_bytes: bytes) -> CodexRunContext:
        try:
            with self._mutex:
                if self._state != "new":
                    raise CodexRuntimeError("runtime preparation is single-use")
                self._state = "preparing"
                with _HELD_LOCK:
                    _HELD.add(self)
                failed = False
                cancelled = False
                try:
                    self._check_cancel()
                    identity = _auth_identity(auth_bytes)
                    self._check_cancel()
                    self._io = _WindowsIO()  # fakes replace this private boundary in pure tests
                    self._layout()
                    for root in (self._cwd, *self._extras, self._data):
                        self._guard(Path(root))
                    self._io.private(Path(self._data))
                    _assert_plain_roots((self._cwd, *self._extras))
                    self._check_cancel()
                    homes = Path(self._data) / "codex-homes-v1"
                    self._directory(homes)
                    self._home = homes / _home_key(self._cwd, self._extras, identity)
                    self._directory(self.home)
                    self._lock = self._io.lock(self.home / "runtime.lock")
                    self._check_cancel()
                    # A stable per-home command scratch scope keeps config bytes fixed.
                    # Auth from crashes is never adopted; only serialized settled runs reuse it.
                    scratch = self.home / "command-scratch"
                    helpers, profile = self.home / "helpers", self.home / "profile"
                    for directory in (scratch, helpers, profile, profile / "AppData",
                                      profile / "AppData" / "Roaming", profile / "AppData" / "Local"):
                        self._directory(directory)
                    # Per-home serialization permits a fixed scratch path and fixed config
                    # bytes. No broader TEMP/helper/profile tree receives write permission.
                    command_temp = scratch
                    content = _render_config(self._cwd, self._extras, str(scratch), (str(helpers), str(profile)))
                    if self._io.exists(self.config_path):
                        self._config = self._io.pin(self.config_path)
                        if self._config.read(len(content)) != content:
                            raise CodexRuntimeError("owned config differs from expected bytes")
                    else:
                        self._create_and_pin(self.config_path, content, auth=False)
                    self._check_cancel()
                    self._create_and_pin(self.home / "auth.json", auth_bytes, auth=True)
                    self._environment = dict(self._env_input)
                    self._environment.update({
                        "CODEX_HOME": str(self.home), "HOME": str(profile), "USERPROFILE": str(profile),
                        "APPDATA": str(profile / "AppData" / "Roaming"),
                        "LOCALAPPDATA": str(profile / "AppData" / "Local"),
                        "TEMP": str(command_temp), "TMP": str(command_temp),
                    })
                    self._check_cancel()
                    self._state = "ready"
                except Exception as exc:  # noqa: BLE001 - sanitize every native/parser failure without chaining secrets
                    failed, cancelled = True, isinstance(exc, CodexRuntimeCancelled)
                except BaseException as interrupt:  # noqa: BLE001 - scrub secret frames before preserving interruption
                    # Even interpreter-level interruptions keep strong references if cleanup
                    # cannot be established. No preparation worker is ever detached here.
                    # Preserve the interruption, but never its secret-bearing lower
                    # write/ctypes frames or parser/cleanup exception chains.
                    interrupt.__traceback__ = None
                    interrupt.__context__ = None
                    interrupt.__cause__ = None
                    self._cleanup_failure()
                    raise interrupt.with_traceback(None) from None
                finally:
                    # Do not retain the supplied snapshot in the preparation frame on errors.
                    auth_bytes = b""
                if failed:
                    cleaned = self._cleanup_failure()
                    if not cleaned:
                        raise CodexRuntimeUnavailable("preparation refused; cleanup uncertain; leases retained")
                    if cancelled:
                        raise CodexRuntimeCancelled("owned Codex preparation cancelled")
                    raise CodexRuntimeError("owned Codex preparation refused")
                return self
        finally:
            auth_bytes = b""

    def _cleanup_failure(self) -> bool:
        try:
            self.close()
        except Exception:  # noqa: BLE001 - uncertainty retains references, never guesses ownership
            self._state = "retained"
            return False
        return True

    def close(self, retain: bool = False) -> None:
        """Join preparation. Caller asserts drained provider unless retain=True.

        No recursive deletion. Only this instance's exact auth file is removed;
        homes/config/scratch persist. A cleanup failure preserves remaining leases.
        """
        if type(retain) is not bool:
            raise CodexRuntimeError("invalid runtime retention request")
        with self._mutex:
            if self._state == "closed":
                return
            if retain:
                self._state = "retained"
                with _HELD_LOCK:
                    _HELD.add(self)
                return
            failed = False
            try:
                if self._io is not None and self._io.unsettled((self._auth, self._config, self._lock)):
                    raise CodexRuntimeError("unadopted native lease; cleanup blocked")
                if self._auth is not None:
                    self._auth.close()
                if self._auth_identity is not None:
                    self._io.remove_owned(self.home / "auth.json", self._auth_identity)
                    self._auth_identity = None
                if self._config is not None:
                    self._config.close()
                if self._lock is not None:
                    self._lock.close()
                if self._io is not None and self._io.unsettled():
                    raise CodexRuntimeError("native file leases remain unsettled")
                self._guards.close()
            except Exception:  # noqa: BLE001 - sanitized report; all remaining leases stay strongly referenced
                failed = True
            if failed:
                self._state = "retained"
                with _HELD_LOCK:
                    _HELD.add(self)
                raise CodexRuntimeError("runtime cleanup uncertain; leases retained")
            self._environment.clear()
            self._state = "closed"
            with _HELD_LOCK:
                _HELD.discard(self)
