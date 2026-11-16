"""Evidence-bound activation gate for owned Codex execution.

Production Codex execution is refused unless `<data_dir>/codex-activation.json` exists and binds three
identities to a complete set of PASSed native containment checks:

* the Codex version string printed by `codex --version`;
* the SHA-256 over every file of the resolved executable (interpreter, npm package tree / native
  vendor tree), see :func:`executable_files`;
* the SHA-256 over the source bytes of the isolation code (:data:`IDENTITY_MODULES`), snapshotted once
  when this module is imported, from the files of the modules this process actually loaded.

The record is written ONLY by :func:`attest`, which runs the model-free native checks against the
real installed binary in a private scratch below the data dir (synthetic git repo, synthetic fake
auth, synthetic canaries; never the user's Codex home or credentials). Every check carries its own
positive control so that a broken harness cannot yield a vacuous PASS. Nothing in this module or in
the adapter ever creates the record implicitly, and there is no environment/request bypass.

Reading is fail closed: size cap, no-follow/no-hardlink read, owner-only ACL, strict schema, exact
check list; any deviation means "not active" with a specific reason.
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import importlib
import json
import os
import re
import secrets
import stat as stat_mod
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from orchestrator.security import acl, paths, worker_job

SCHEMA_VERSION = 1
RECORD_NAME = "codex-activation.json"
MAX_RECORD_BYTES = 64 * 1024
REFUSAL_PREFIX = "Codex execution blocked: owned runtime activation"
PASS, FAIL, ERROR = "PASS", "FAIL", "ERROR"

# The exact, ordered list of native checks a record must carry (all PASS).
REQUIRED_CHECKS: tuple[str, ...] = (
    "sandbox_positive_control",
    "owned_home_read_denied",
    "owned_home_write_denied",
    "sibling_read_denied",
    "sibling_write_denied",
    "read_profile_write_denied",
    "late_project_mcp_ignored",
    "planted_hardlink_refused",
    "metadata_hardlink_file_refused",
    "metadata_hardlink_directory_refused",
    "worker_hardlink_contained",
    "worker_junction_contained",
)

# Modules whose source defines the attested isolation behaviour (kept to the isolation-relevant set, so that
# unrelated edits do not invalidate records). The activation module itself is included: a change to the gate
# or to the checks invalidates every earlier record.
IDENTITY_MODULES: tuple[tuple[str, str], ...] = (
    ("security/codex_runtime.py", "orchestrator.security.codex_runtime"),
    ("security/codex_auth.py", "orchestrator.security.codex_auth"),
    ("security/codex_activation.py", "orchestrator.security.codex_activation"),
    ("security/paths.py", "orchestrator.security.paths"),
    ("security/acl.py", "orchestrator.security.acl"),
    ("security/worker_job.py", "orchestrator.security.worker_job"),
    ("providers/codex.py", "orchestrator.providers.codex"),
    ("providers/process.py", "orchestrator.providers.process"),
    ("gitops/git.py", "orchestrator.gitops.git"),
)

_MAX_EXE_FILES = 4096
_MAX_EXE_BYTES = 4 * 1024 ** 3
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_VERSION = re.compile(r"^[\x20-\x7e]{1,200}$")
_RECORD_KEYS = frozenset({"schema_version", "codex_version", "executable_sha256", "executable_path",
                          "code_identity_sha256", "checks", "attested_at"})


class ActivationUnavailable(RuntimeError):
    """The current binary/code identity cannot be established (fail closed)."""


class ActivationRecordInvalid(ValueError):
    """The record is malformed, tampered with or not trustworthy."""


# --------------------------------------------------------------------------- code identity

_code_identity: str | None = None
_code_identity_error: str | None = None


def _snapshot_sources(modules=IDENTITY_MODULES) -> tuple[tuple[str, int, str], ...]:
    """(label, size, sha256) of the source file of every identity module, imported here if necessary.

    The file is the loaded module's own `__file__`; a module without a plain `.py` source is refused.
    """
    out = []
    for label, name in modules:
        module = importlib.import_module(name)
        source = getattr(module, "__file__", None)
        if not isinstance(source, str) or not source.lower().endswith(".py"):
            raise ActivationUnavailable(f"isolation module has no source file: {label}")
        data = Path(source).read_bytes()
        out.append((label, len(data), hashlib.sha256(data).hexdigest()))
    return tuple(out)


def _compute_code_identity(entries) -> str:
    digest = hashlib.sha256(b"orchestrator-codex-isolation-code-v2\n")
    for label, size, sha in entries:
        digest.update(f"{label}\0{size}\0{sha}\n".encode("ascii"))
    return digest.hexdigest()


def _load_code_identity() -> None:
    """Called exactly once, at the end of this module's import: every identity module is loaded at that
    moment and its source bytes are read then. Later edits on disk never change the identity of a running
    process, so a server running old code cannot match a record attested for new code (and vice versa)."""
    global _code_identity, _code_identity_error
    try:
        _code_identity = _compute_code_identity(_snapshot_sources())
    except Exception as exc:  # noqa: BLE001 - fail closed in code_identity(); never break the import
        _code_identity_error = f"{exc.__class__.__name__}: {exc}"[:200]


def code_identity() -> str:
    """Identity of the isolation source this process loaded (import-time snapshot; never re-read)."""
    if _code_identity is None:
        raise ActivationUnavailable(f"isolation source identity unavailable ({_code_identity_error or 'not loaded'})")
    return _code_identity


# --------------------------------------------------------------------------- executable identity

def _plain_file(path: Path) -> os.stat_result:
    st = os.stat(path, follow_symlinks=False)
    if paths._is_link(st) or not stat_mod.S_ISREG(st.st_mode):
        raise ActivationUnavailable(f"executable component is not a plain file: {path.name}")
    return st


def _tree(root: Path, label: str) -> list[tuple[str, Path]]:
    st = os.stat(root, follow_symlinks=False)
    if paths._is_link(st) or not stat_mod.S_ISDIR(st.st_mode):
        raise ActivationUnavailable("executable package root is not a plain directory")
    out: list[tuple[str, Path]] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                est = os.stat(entry.path, follow_symlinks=False)
                if paths._is_link(est):
                    raise ActivationUnavailable("executable package contains a link; unsupported layout")
                if stat_mod.S_ISDIR(est.st_mode):
                    stack.append(Path(entry.path))
                elif stat_mod.S_ISREG(est.st_mode):
                    rel = Path(entry.path).relative_to(root).as_posix()
                    out.append((f"{label}:{rel}", Path(entry.path)))
                else:
                    raise ActivationUnavailable("executable package contains a special file")
                if len(out) > _MAX_EXE_FILES:
                    raise ActivationUnavailable("executable package exceeds the file bound")
    return out


def _is_vendored_native(label: str) -> bool:
    kind, _, rel = label.partition(":")
    parts = rel.lower().split("/")
    return (not kind.startswith("argv") and parts[-1] in ("codex.exe", "codex")
            and len(parts) >= 3 and parts[-2] == "bin" and "vendor" in parts[:-2])


def executable_files(exe) -> list[tuple[str, Path]]:
    """Every file whose bytes define the launched program, labelled install-location independently.

    * npm shim (`node`, `<pkg>/bin/codex.js`): the interpreter plus the whole package tree and any
      hoisted sibling platform package (`<scope>/<pkg>-*`). The vendored native `codex.exe`
      (`.../vendor/.../bin/codex.exe`) and its sandbox helpers must be among them, else refused.
    * native `.../<triple>/bin/codex.exe` with a sibling `codex-resources`: that whole triple tree.
    * any other single native executable: that file.
    """
    prefix = tuple(exe.argv_prefix)
    if not 1 <= len(prefix) <= 2:
        raise ActivationUnavailable("unsupported executable layout")
    files: list[tuple[str, Path]] = []
    for index, raw in enumerate(prefix):
        path = Path(raw)
        if not path.is_absolute():
            raise ActivationUnavailable("executable path is not absolute")
        _plain_file(path)
        files.append((f"argv{index}:{path.name.lower()}", path))
    if len(prefix) == 2:
        entry = Path(prefix[1])
        root = entry.parent.parent
        if (entry.suffix.lower() not in (".js", ".mjs", ".cjs") or entry.parent.name.lower() != "bin"
                or not (root / "package.json").is_file()):
            raise ActivationUnavailable("unsupported script executable layout")
        files += _tree(root, "package")
        scope = root.parent
        if scope.name.startswith("@"):
            # npm may hoist the platform package (`@openai/codex-win32-x64`) next to the main package
            # instead of nesting it below `node_modules/`; `require.resolve` finds either.
            with os.scandir(scope) as entries:
                hoisted = sorted(e.name for e in entries if e.name.lower().startswith(root.name.lower() + "-"))
            for name in hoisted:
                files += _tree(scope / name, f"hoisted/{name.lower()}")
        if not any(_is_vendored_native(label) for label, _ in files):
            raise ActivationUnavailable("vendored native codex executable not found in the package tree")
    else:
        native = Path(prefix[0])
        triple = native.parent.parent
        if native.parent.name.lower() == "bin" and (triple / "codex-resources").is_dir():
            files += _tree(triple, "vendor")
    files.sort(key=lambda item: item[0])
    return files


def _change_time(path: Path, st: os.stat_result) -> int:
    """NTFS change time: updated on every data, attribute, rename or link change and not restored by
    `os.utime`, unlike the modification time. POSIX: st_ctime_ns."""
    if os.name != "nt":
        return st.st_ctime_ns
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.GetFileInformationByHandleEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    k32.GetFileInformationByHandleEx.restype = ctypes.c_int
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    text = os.path.abspath(str(path))
    if not text.startswith("\\\\"):
        text = "\\\\?\\" + text
    # FILE_READ_ATTRIBUTES; share all; OPEN_EXISTING; BACKUP_SEMANTICS | OPEN_REPARSE_POINT (never follow)
    handle = k32.CreateFileW(text, 0x80, 7, None, 3, 0x02000000 | 0x00200000, None)
    if not handle or handle == ctypes.c_void_p(-1).value:
        raise ActivationUnavailable(f"executable component cannot be inspected: {path.name}")
    try:
        basic = (ctypes.c_longlong * 5)()  # FILE_BASIC_INFO: creation, access, write, change, attributes
        if not k32.GetFileInformationByHandleEx(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)):
            raise ActivationUnavailable(f"executable component cannot be inspected: {path.name}")
        return int(basic[3])
    finally:
        k32.CloseHandle(handle)


def fingerprint(files: list[tuple[str, Path]]) -> tuple:
    out = []
    for label, path in files:
        st = _plain_file(path)
        out.append((label, os.path.normcase(str(path)), st.st_size, st.st_mtime_ns, st.st_ino,
                    st.st_ctime_ns, _change_time(path, st)))
    return tuple(out)


def _hash_file(path: Path, expected: os.stat_result) -> str:
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        st = os.fstat(fd)
        if st.st_size != expected.st_size or st.st_mtime_ns != expected.st_mtime_ns:
            raise ActivationUnavailable("executable changed while hashing")
        while True:
            chunk = os.read(fd, 4 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()


def executable_sha256(files: list[tuple[str, Path]]) -> str:
    digest = hashlib.sha256(b"orchestrator-codex-executable-v1\n")
    total = 0
    for label, path in files:
        st = _plain_file(path)
        total += st.st_size
        if total > _MAX_EXE_BYTES:
            raise ActivationUnavailable("executable exceeds the size bound")
        digest.update(f"{label}\0{st.st_size}\0{_hash_file(path, st)}\n".encode())
    return digest.hexdigest()


def parse_version(stdout: str) -> str | None:
    for line in stdout.splitlines():
        line = line.strip()
        if line:
            return line if _VERSION.match(line) and re.search(r"\d+\.\d+\.\d+", line) else None
    return None


async def probe_version(exe, env: dict[str, str]) -> str | None:
    """`codex --version` (model-free). None when it fails or prints something unexpected."""
    from orchestrator.providers.process import run_capture

    code, out, _ = await run_capture([*exe.argv_prefix, "--version"], env=env, timeout=30)
    return parse_version(out) if code == 0 else None


@dataclass
class BinaryIdentity:
    version: str
    executable_sha256: str
    path: str
    fingerprint: tuple = ()  # stat fingerprint the hash was taken under (not part of the record)


class BinaryIdentityCache:
    """Per-adapter cache keyed by the stat fingerprint of every executable file."""

    def __init__(self) -> None:
        self._key: tuple | None = None
        self._value: BinaryIdentity | None = None

    async def current(self, exe, env: dict[str, str], *, version_probe=None) -> BinaryIdentity:
        files = await asyncio.to_thread(executable_files, exe)
        key = await asyncio.to_thread(fingerprint, files)
        if self._key == key and self._value is not None:
            return self._value
        sha = await asyncio.to_thread(executable_sha256, files)
        version = await (version_probe or probe_version)(exe, env)
        if version is None:
            raise ActivationUnavailable("codex --version failed or printed an unexpected version")
        if await asyncio.to_thread(fingerprint, files) != key:
            raise ActivationUnavailable("executable changed during identity check")
        self._key, self._value = key, BinaryIdentity(version, sha, str(exe.path), key)
        return self._value


# --------------------------------------------------------------------------- record

def record_path(data_dir: str | os.PathLike[str]) -> Path:
    return Path(data_dir) / RECORD_NAME


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ActivationRecordInvalid("duplicate key")
        result[key] = value
    return result


def _reject_constant(_name):
    raise ActivationRecordInvalid("non-finite number")


def validate_record(obj: Any) -> dict[str, Any]:
    if type(obj) is not dict or set(obj) != _RECORD_KEYS:
        raise ActivationRecordInvalid("unexpected record fields")
    if type(obj["schema_version"]) is not int or obj["schema_version"] != SCHEMA_VERSION:
        raise ActivationRecordInvalid("unsupported schema version")
    version = obj["codex_version"]
    if type(version) is not str or not _VERSION.match(version):
        raise ActivationRecordInvalid("invalid codex version")
    for key in ("executable_sha256", "code_identity_sha256"):
        if type(obj[key]) is not str or not _HEX64.match(obj[key]):
            raise ActivationRecordInvalid(f"invalid {key}")
    exe_path = obj["executable_path"]
    if type(exe_path) is not str or not 0 < len(exe_path) <= 4096 or any(ord(c) < 32 for c in exe_path):
        raise ActivationRecordInvalid("invalid executable path")
    checks = obj["checks"]
    if type(checks) is not list or len(checks) != len(REQUIRED_CHECKS):
        raise ActivationRecordInvalid("incomplete check list")
    for expected, item in zip(REQUIRED_CHECKS, checks, strict=True):
        if type(item) is not dict or set(item) != {"name", "result"}:
            raise ActivationRecordInvalid("invalid check entry")
        if item["name"] != expected or item["result"] != PASS:
            raise ActivationRecordInvalid("check list is not a complete PASS")
    stamp = obj["attested_at"]
    if type(stamp) is not str or not _STAMP.match(stamp):
        raise ActivationRecordInvalid("invalid attestation timestamp")
    try:
        when = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ActivationRecordInvalid("invalid attestation timestamp") from exc
    if when > datetime.now(UTC) + timedelta(minutes=5):
        raise ActivationRecordInvalid("attestation timestamp is in the future")
    return obj


def parse_record(data: bytes) -> dict[str, Any]:
    if type(data) is not bytes or not 0 < len(data) <= MAX_RECORD_BYTES:
        raise ActivationRecordInvalid("record size out of bounds")
    try:
        obj = json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        if isinstance(exc, ActivationRecordInvalid):
            raise
        raise ActivationRecordInvalid("record is not valid JSON") from None
    return validate_record(obj)


def read_record(data_dir: str | os.PathLike[str]) -> tuple[dict[str, Any] | None, str]:
    """(record, "") when a trustworthy record exists; (None, reason) otherwise. Never raises."""
    path = record_path(data_dir)
    try:
        data = paths.read_inside(data_dir, RECORD_NAME, max_bytes=MAX_RECORD_BYTES)
    except FileNotFoundError:
        return None, "missing: no attestation record (run `orchestrator codex-attest`)"
    except paths.FileTooLarge:
        return None, "invalid: attestation record exceeds the size bound"
    except (OSError, paths.PathPolicyError):
        return None, "invalid: attestation record is not a plain, singly linked file"
    problems = acl.check_private(path)
    if problems:
        return None, "invalid: attestation record is not owner-only"
    try:
        return parse_record(data), ""
    except ActivationRecordInvalid as exc:
        return None, f"invalid: attestation record rejected ({exc})"


def write_record(data_dir: str | os.PathLike[str], record: dict[str, Any]) -> Path:
    """Atomically replace the record with an owner-only file. Only :func:`attest` calls this."""
    data = (json.dumps(validate_record(record), indent=2, sort_keys=True) + "\n").encode("utf-8")
    parse_record(data)
    final = record_path(data_dir)
    tmp = Path(data_dir) / f".{RECORD_NAME}.{secrets.token_hex(6)}.tmp"
    # The guard (plain directory chain) is held while the temporary file is created; it denies write
    # sharing on the directory, so the rename below happens after it is released.
    with paths.directory_guard(data_dir):
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        if not acl.harden_private_file(tmp) or acl.check_private(tmp):
            raise OSError("attestation record could not be made owner-only")
        os.replace(tmp, final)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return final


def evaluate(record: dict[str, Any], identity: BinaryIdentity, code_sha: str) -> str:
    """"" when the record matches the current binary and source; otherwise a specific stale reason."""
    if record["codex_version"] != identity.version:
        return (f"stale: attested for {record['codex_version']!r}, installed is {identity.version!r} "
                "(run `orchestrator codex-attest`)")
    if record["executable_sha256"] != identity.executable_sha256:
        return "stale: executable hash differs from the attested binary (run `orchestrator codex-attest`)"
    if record["code_identity_sha256"] != code_sha:
        return "stale: isolation source changed since attestation (run `orchestrator codex-attest`)"
    return ""


async def check_activation(data_dir, exe, env: dict[str, str], cache: BinaryIdentityCache,
                           *, version_probe=None) -> tuple[str, BinaryIdentity | None]:
    """("", identity) when owned execution is activated for this binary and source, else (reason, None)."""
    record, reason = read_record(data_dir)
    if record is None:
        return reason, None
    try:
        identity = await cache.current(exe, env, version_probe=version_probe)
        code_sha = code_identity()
    except ActivationUnavailable as exc:
        return f"unavailable: {exc}", None
    except OSError:
        return "unavailable: executable identity could not be read", None
    reason = evaluate(record, identity, code_sha)
    return reason, (None if reason else identity)


async def activation_refusal(data_dir, exe, env: dict[str, str], cache: BinaryIdentityCache,
                             *, version_probe=None) -> str:
    """"" when owned execution is activated for this binary and source, else the refusal reason."""
    return (await check_activation(data_dir, exe, env, cache, version_probe=version_probe))[0]


def unchanged_since(exe, identity: BinaryIdentity) -> bool:
    """True when every file of `exe` still carries the stat fingerprint `identity` was hashed under."""
    try:
        return bool(identity.fingerprint) and fingerprint(executable_files(exe)) == identity.fingerprint
    except (ActivationUnavailable, OSError):
        return False


def refusal_message(reason: str) -> str:
    return f"{REFUSAL_PREFIX} {reason}"


# --------------------------------------------------------------------------- attestation


@dataclass
class CheckResult:
    name: str
    result: str
    detail: str = ""


@dataclass
class AttestationResult:
    recorded: bool
    checks: list[CheckResult] = field(default_factory=list)
    record_path: str | None = None
    codex_version: str | None = None
    executable_sha256: str | None = None
    code_identity_sha256: str | None = None
    cleanup: str = "not started"
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"recorded": self.recorded, "record_path": self.record_path, "codex_version": self.codex_version,
                "executable_sha256": self.executable_sha256, "code_identity_sha256": self.code_identity_sha256,
                "cleanup": self.cleanup, "error": self.error,
                "checks": [{"name": c.name, "result": c.result, "detail": c.detail} for c in self.checks]}


def _fake_auth() -> bytes:
    """Synthetic subscription-shaped snapshot. Never the user's credentials."""
    return json.dumps({"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": {
        "access_token": "SYNTHETIC-ATTEST-ACCESS", "refresh_token": "SYNTHETIC-ATTEST-REFRESH",
        "id_token": "SYNTHETIC-ATTEST-ID", "account_id": "synthetic-attest-account"}}).encode()


class _Probe(Exception):
    """A positive control or a refusal assertion failed; message is the check detail."""


_CMD_UNSAFE = re.compile(r'[\s"&|<>^%!()]')


def _p(path: Path) -> str:
    """A path for a `cmd /c` command line WITHOUT quotes.

    Python (list2cmdline) escapes an embedded double quote as \" and cmd.exe does not understand that
    escape: a quoted path makes the command fail with a syntax error, which would turn a denial check
    into a vacuous PASS. Paths that would need quotes are refused instead (the harness fails closed).
    """
    text = str(path)
    if _CMD_UNSAFE.search(text):
        raise _Probe("harness path needs cmd quoting; unsupported (use a data dir without spaces/metacharacters)")
    return text


MCP_START_WINDOW = 15.0
MCP_ATTEMPTS = 2
# The scratch adds ~12 characters and the owned home below it ~110 more (r<n>\codex-homes-v1\<sha256>\profile...).
MAX_DATA_DIR_CHARS = 120


async def _appears(path: Path, window: float = MCP_START_WINDOW) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + window
    while True:
        if path.is_file() and path.stat().st_size:
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.25)


def _tail(text: str, limit: int = 200) -> str:
    """Bounded single-line diagnostic tail (synthetic fixture output only)."""
    return " ".join((text or "").split())[-limit:]


def _expect(condition: bool, detail: str) -> None:
    if not condition:
        raise _Probe(detail)


def _denied(code) -> bool:
    """A denial is a completed process with a nonzero exit. `run_capture` reports a timeout or a spawn
    failure as code None: that is a harness failure, never evidence of a denial."""
    if not isinstance(code, int) or isinstance(code, bool):
        raise _Probe(f"harness: command did not complete (exit={code!r}); denial not established")
    return code != 0


class _Harness:
    def __init__(self, scratch: Path, adapter, prefix: tuple[str, ...]):
        from orchestrator.security import codex_runtime as rt

        self.rt = rt
        self.scratch = scratch
        self.adapter = adapter
        self.prefix = prefix
        self.io = rt._WindowsIO()
        self.cmd = str(Path(os.environ.get("SYSTEMROOT") or r"C:\Windows") / "System32" / "cmd.exe")
        self.contexts: list = []
        self.worktrees: list[Path] = []
        self.retained = False
        self.last_err = ""
        self._n = 0
        self.repo = self.base = None

    def private_dir(self, name: str) -> Path:
        path = self.scratch / name
        with paths.directory_guard(self.scratch):
            self.io.directory(path, exclusive=True)
        return path

    def setup_repo(self) -> None:
        from orchestrator.gitops import git

        self.repo = self.private_dir("repo")
        (self.repo / "README.md").write_text("synthetic attestation fixture\n", encoding="ascii")
        self.base = git.init_repo(self.repo)
        self.private_dir("w")

    def worktree(self, name: str) -> Path:
        from orchestrator.gitops import worktrees

        info = worktrees.create(self.repo, self.scratch / "w" / name, f"attest-{name}", self.base)
        path = Path(info.path)
        self.worktrees.append(path)
        return path

    def context(self, task: Path):
        self._n += 1
        data = self.private_dir(f"r{self._n}")
        ctx = self.rt.CodexRunContext(data, str(task), (), self.adapter.child_env())
        self.contexts.append(ctx)
        return ctx

    def close(self, ctx) -> None:
        try:
            ctx.close()
        except self.rt.CodexRuntimeUnavailable:
            self.retained = True
            raise

    async def sandbox(self, ctx, task: Path, command: str, profile: str = "orch-write"):
        from orchestrator.providers.process import run_capture

        outcome = await run_capture([*self.prefix, "--disable", "hooks", "--disable", "plugins", "--disable", "apps",
                                     "sandbox", "--include-managed-config", "-P", profile, "-C", str(task), "--",
                                     self.cmd, "/d", "/c", command], env=ctx.environment, cwd=str(task), timeout=90)
        self.last_err = outcome[2]
        return outcome

    async def host(self, command: str, cwd: Path):
        from orchestrator.providers.process import run_capture

        return await run_capture([self.cmd, "/d", "/c", command], cwd=str(cwd), timeout=30)

    def prepare_control(self, task: Path) -> None:
        """Positive control for refusal checks: the same layout prepares while the task is still clean,
        so a later refusal is attributable to the planted alias, not to the harness."""
        ctx = self.context(task)
        try:
            ctx.prepare(_fake_auth())
        except self.rt.CodexRuntimeUnavailable:
            raise _Probe("positive control: clean task preparation refused") from None
        self.close(ctx)
        _expect(not (ctx.home / "auth.json").exists(), "positive control: synthetic auth not removed on close")

    async def sandbox_control(self, ctx, task: Path, tag: str) -> None:
        """Positive control: the profile starts, writes inside the task and reads an in-task file."""
        code, out, _ = await self.sandbox(ctx, task, f"echo ORCH_ATTEST_READY_{tag}")
        _expect(code == 0 and f"ORCH_ATTEST_READY_{tag}" in out, "positive control: sandbox startup failed")
        target = task / f"control-{tag}.txt"
        code, _, err = await self.sandbox(ctx, task, f'echo ORCH_ATTEST_WRITE_{tag}>{_p(target)}')
        _expect(code == 0 and target.is_file() and target.read_text().strip() == f"ORCH_ATTEST_WRITE_{tag}",
                f"positive control: in-task absolute write failed (exit={code}; {_tail(err)})")
        readable = task / f"readable-{tag}.txt"
        readable.write_text(f"ORCH_ATTEST_READABLE_{tag}\n", encoding="ascii")
        code, out, err = await self.sandbox(ctx, task, f'type {_p(readable)}')
        _expect(code == 0 and f"ORCH_ATTEST_READABLE_{tag}" in out,
                f"positive control: in-task absolute read failed (exit={code}; {_tail(err)})")


async def _run_checks(h: _Harness, only: frozenset[str] | None = None) -> dict[str, CheckResult]:
    """Run every native check group (or the groups covering `only`, for diagnostics); results by name."""
    rt = h.rt
    results: dict[str, CheckResult] = {}

    def record(name: str, result: str, detail: str = "") -> None:
        results[name] = CheckResult(name, result, detail)

    async def group(names: tuple[str, ...], body) -> None:
        if only is not None and not only.intersection(names):
            return
        try:
            await body(record)
        except _Probe as exc:
            for name in names:
                results.setdefault(name, CheckResult(name, FAIL, str(exc)))
        except rt.CodexRuntimeUnavailable as exc:
            for name in names:
                results.setdefault(name, CheckResult(name, ERROR, f"owned runtime refused: {exc}"))
        except worker_job.ContainmentError:
            h.retained = True  # process lifetime unknown: never clean up or record
            raise
        except Exception as exc:  # noqa: BLE001 - a harness failure is never a PASS
            for name in names:
                results.setdefault(name, CheckResult(name, ERROR, f"{exc.__class__.__name__}: {exc}"[:300]))

    # ---- group A: named profiles (positive control, owned home, sibling, read-only profile)
    async def profiles(rec):
        task, sibling = h.worktree("profile-task"), h.worktree("profile-sibling")
        (sibling / "private.txt").write_text("SYNTHETIC-SIBLING", encoding="ascii")
        ctx = h.context(task)
        ctx.prepare(_fake_auth())
        try:
            canary = ctx.home / "fake-private.txt"
            canary.write_text("SYNTHETIC-PRIVATE", encoding="ascii")
            await h.sandbox_control(ctx, task, "A")
            rec("sandbox_positive_control", PASS, "startup, in-task absolute write and read succeeded")

            code, out, err = await h.sandbox(ctx, task, f'type {_p(canary)}')
            ok = _denied(code) and "SYNTHETIC-PRIVATE" not in out + err
            rec("owned_home_read_denied", PASS if ok else FAIL, f"exit={code}; {_tail(h.last_err, 80)}")
            target = ctx.home / "must-not-write.txt"
            code, _, _ = await h.sandbox(ctx, task, f'echo BAD>{_p(target)}')
            ok = _denied(code) and not target.exists()
            rec("owned_home_write_denied", PASS if ok else FAIL, f"exit={code}; {_tail(h.last_err, 80)}")
            code, out, err = await h.sandbox(ctx, task, f'type {_p(sibling / "private.txt")}')
            ok = _denied(code) and "SYNTHETIC-SIBLING" not in out + err
            rec("sibling_read_denied", PASS if ok else FAIL, f"exit={code}; {_tail(h.last_err, 80)}")
            target = sibling / "must-not-write.txt"
            code, _, _ = await h.sandbox(ctx, task, f'echo BAD>{_p(target)}')
            ok = _denied(code) and not target.exists()
            rec("sibling_write_denied", PASS if ok else FAIL, f"exit={code}; {_tail(h.last_err, 80)}")
            # The sandbox must still work after the denials, or they may reflect a broken sandbox.
            await h.sandbox_control(ctx, task, "A2")

            readable = task / "readable-A.txt"
            code, out, _ = await h.sandbox(ctx, task, f'type {_p(readable)}', "orch-read")
            _expect(code == 0 and "ORCH_ATTEST_READABLE_A" in out, "positive control: read-only profile read failed")
            target = task / "readonly-must-not-write.txt"
            code, _, _ = await h.sandbox(ctx, task, f'echo BAD>{_p(target)}', "orch-read")
            ok = _denied(code) and not target.exists()
            rec("read_profile_write_denied", PASS if ok else FAIL, f"exit={code}; {_tail(h.last_err, 80)}")
            code, out, _ = await h.sandbox(ctx, task, f'type {_p(readable)}', "orch-read")
            _expect(code == 0 and "ORCH_ATTEST_READABLE_A" in out,
                    "positive control: read-only profile read failed after the denial")
            if (canary.read_text() != "SYNTHETIC-PRIVATE"
                    or (sibling / "private.txt").read_text() != "SYNTHETIC-SIBLING"):
                for name in ("owned_home_read_denied", "owned_home_write_denied",
                             "sibling_read_denied", "sibling_write_denied"):
                    rec(name, FAIL, "synthetic canary modified")
        finally:
            h.close(ctx)
        if (ctx.home / "auth.json").exists():
            rec("sandbox_positive_control", FAIL, "synthetic auth not removed on close")

    await group(("sandbox_positive_control", "owned_home_read_denied", "owned_home_write_denied",
                 "sibling_read_denied", "sibling_write_denied", "read_profile_write_denied"), profiles)

    # ---- group B: late untrusted project MCP must not start; trusted fixture positive control must
    async def late_mcp(rec):
        """Each attempt is a complete negative + positive pair on a fresh worktree and home. An untrusted
        start in ANY attempt is a FAIL; PASS needs one attempt whose positive control started. A failed
        positive control only makes that attempt inconclusive (observed flaky on Codex 0.169.0 under load)."""
        failures = []
        for attempt in range(1, MCP_ATTEMPTS + 1):
            try:
                if await late_mcp_attempt(attempt):
                    rec("late_project_mcp_ignored", PASS,
                        f"untrusted not started; trusted control started (attempt {attempt})")
                else:
                    rec("late_project_mcp_ignored", FAIL, f"untrusted project MCP server started (attempt {attempt})")
                return
            except _Probe as exc:
                failures.append(f"attempt {attempt}: {exc}")
        raise _Probe("; ".join(failures))

    async def late_mcp_attempt(attempt: int) -> bool:
        from orchestrator.providers.process import run_capture

        task = h.worktree(f"mcp-task-{attempt}")
        ctx = h.context(task)
        ctx.prepare(_fake_auth())
        try:
            environment = ctx.environment
            inventory = await h.adapter._mcp_inventory(h.prefix, environment, cwd=str(task))
            _expect(inventory == (), f"effective MCP inventory not empty: {inventory!r}")
            marker = task.parent / f"controlled-mcp-started-{attempt}"
            program = task.parent / f"controlled_mcp_{attempt}.py"
            program.write_text('import pathlib,sys\npathlib.Path(sys.argv[1]).write_text("STARTED")\n',
                               encoding="ascii")
            (task / ".codex").mkdir()
            (task / ".codex" / "config.toml").write_text(
                '[mcp_servers."late.controlled"]\ncommand=' + json.dumps(sys.executable)
                + '\nargs=' + json.dumps([str(program), str(marker)]) + '\n', encoding="utf-8")
            argv = [*h.prefix, "--disable", "hooks", "--disable", "plugins", "--disable", "apps",
                    "debug", "prompt-input"]
            code, _, err = await run_capture(argv, env=environment, cwd=str(task), timeout=60)
            _expect(code == 0, f"debug prompt-input failed (exit={code}; {_tail(err)})")
            # MCP servers start asynchronously; the negative and the positive control observe the SAME
            # bounded window, so a slow server start cannot make the negative check vacuous.
            started = await _appears(marker)
            config = ctx.config_path.read_bytes()
        finally:
            h.close(ctx)
        if started:
            return False
        _expect(b'trust_level = "untrusted"' in config, "pinned config lacks the untrusted project entry")
        ctx.config_path.write_bytes(config.replace(b'trust_level = "untrusted"', b'trust_level = "trusted"'))
        code, out, err = await run_capture(argv, env=environment, cwd=str(task), timeout=60)
        appeared = await _appears(marker)
        if not (code == 0 and appeared and marker.read_text() == "STARTED"):
            names = await h.adapter._mcp_inventory(h.prefix, environment, cwd=str(task))
            raise _Probe(f"positive control: trusted fixture MCP server did not start (exit={code}; "
                         f"prompt bytes={len(out)}; trusted inventory={names!r}; {_tail(err)})")
        return True

    await group(("late_project_mcp_ignored",), late_mcp)

    # ---- group C: planted hardlink refused before home/auth creation
    async def planted(rec):
        task = h.worktree("planted-task")
        h.prepare_control(task)
        outside = h.scratch / "w" / "outside-hardlink-canary.txt"
        outside.write_text("SYNTHETIC-OUTSIDE", encoding="ascii")
        alias = task / "planted-hardlink.txt"
        os.link(outside, alias)
        _expect(alias.read_text() == "SYNTHETIC-OUTSIDE" and os.stat(outside).st_nlink == 2,
                "positive control: hardlink not established")
        before = acl.read_security(outside)
        ctx = h.context(task)
        refused = False
        try:
            ctx.prepare(_fake_auth())
        except rt.CodexRuntimeUnavailable:
            refused = True
        if not refused:
            h.close(ctx)
        ok = (refused and ctx._home is None and outside.read_text() == "SYNTHETIC-OUTSIDE"
              and acl.read_security(outside) == before)
        rec("planted_hardlink_refused", PASS if ok else FAIL, "refused before home creation" if ok else "not refused")

    await group(("planted_hardlink_refused",), planted)

    # ---- group D: hardlinked .git metadata (file and directory variants)
    for name, as_directory in (("metadata_hardlink_file_refused", False),
                               ("metadata_hardlink_directory_refused", True)):
        async def metadata(rec, name=name, as_directory=as_directory):
            task = h.worktree(name.replace("_", "-"))
            h.prepare_control(task)
            outside = h.scratch / "w" / f"{name}-canary.txt"
            outside.write_text("SYNTHETIC-METADATA-CANARY", encoding="ascii")
            meta = task / ".git"
            meta.rename(task / "original-gitlink.saved")
            if as_directory:
                meta.mkdir()
                meta = meta / "alias.txt"
            os.link(outside, meta)
            _expect(meta.read_text() == "SYNTHETIC-METADATA-CANARY", "positive control: hardlink not established")
            ctx = h.context(task)
            refused = False
            try:
                ctx.prepare(_fake_auth())
            except rt.CodexRuntimeUnavailable:
                refused = True
            if not refused:
                h.close(ctx)
            ok = (refused and ctx._home is None and ctx not in rt._HELD
                  and outside.read_text() == "SYNTHETIC-METADATA-CANARY")
            rec(name, PASS if ok else FAIL, "refused before home creation" if ok else "not refused")

        await group((name,), metadata)

    # ---- group E: worker-created hardlink to an outside file
    async def worker_hardlink(rec):
        task = h.worktree("hardlink-task")
        outside = h.scratch / "w" / "active-link-canary.txt"
        outside.write_text("SYNTHETIC-ACTIVE-LINK", encoding="ascii")
        before = acl.read_security(outside)
        ctx = h.context(task)
        ctx.prepare(_fake_auth())
        try:
            await h.sandbox_control(ctx, task, "E")
            # The prepared context holds the task ROOT without write sharing, which makes every link/rename
            # INTO the root fail for any process. All link checks therefore run in a subdirectory created
            # after preparation, where only the sandbox policy decides.
            sub = task / "linkdir"
            sub.mkdir()
            host_alias = sub / "host-hardlink-positive.txt"
            code, _, _ = await h.host(f'mklink /H {_p(host_alias)} {_p(outside)}', task)
            _expect(code == 0 and host_alias.read_text() == "SYNTHETIC-ACTIVE-LINK",
                    "positive control: host hardlink creation at the probe location failed")
            host_alias.unlink()
            source = sub / "in-task-source.txt"
            code, _, err = await h.sandbox(ctx, task, f'echo IN_TASK_LINK>{_p(source)}')
            _expect(code == 0 and source.is_file(), f"positive control: worker write in probe dir failed ({_tail(err)})")
            # Informational: whether the profile permits link creation at all (observed: it does not, even
            # in-task). Attribution of the outside denial rests on the two controls above at this location:
            # the host can link here (no guard/sharing/syntax obstacle) and the worker can write here.
            inside = sub / "in-task-link.txt"
            code, _, _ = await h.sandbox(ctx, task, f'mklink /H {_p(inside)} {_p(source)}')
            in_task = f"in-task mklink /H exit={code} created={os.path.lexists(inside)}"
            created = sub / "created-cross-scope.txt"
            code, _, _ = await h.sandbox(ctx, task, f'mklink /H {_p(created)} {_p(outside)}')
            detail = f"{in_task}; worker mklink /H outside exit={code}; {_tail(h.last_err, 80)}"
            if os.path.lexists(created):
                code, out, err = await h.sandbox(ctx, task, f"type {_p(created)}")
                ok = _denied(code) and "SYNTHETIC-ACTIVE-LINK" not in out + err
                detail += f"; read exit={code}"
                code, _, _ = await h.sandbox(ctx, task, f"echo BAD>{_p(created)}")
                ok = _denied(code) and ok
                detail += f"; write exit={code}"
            else:
                ok = _denied(code)
            await h.sandbox_control(ctx, task, "E2")
        finally:
            h.close(ctx)
        ok = ok and outside.read_text() == "SYNTHETIC-ACTIVE-LINK" and acl.read_security(outside) == before
        rec("worker_hardlink_contained", PASS if ok else FAIL, detail)

    await group(("worker_hardlink_contained",), worker_hardlink)

    # ---- group F: worker-created directory junction to an outside / sibling directory
    async def worker_junction(rec):
        task, sibling = h.worktree("junction-task"), h.worktree("junction-sibling")
        (sibling / "secret.txt").write_text("SYNTHETIC-JUNCTION-SIBLING", encoding="ascii")
        outside = h.private_dir("outside-dir")
        (outside / "secret.txt").write_text("SYNTHETIC-JUNCTION-OUTSIDE", encoding="ascii")
        ctx = h.context(task)
        ctx.prepare(_fake_auth())
        details, ok = [], True
        try:
            await h.sandbox_control(ctx, task, "F")
            sub = task / "junctiondir"  # unguarded probe location (see group E)
            sub.mkdir()
            host_junction = sub / "host-junction-positive"
            code, _, _ = await h.host(f'mklink /J {_p(host_junction)} {_p(outside)}', task)
            _expect(code == 0 and (host_junction / "secret.txt").read_text() == "SYNTHETIC-JUNCTION-OUTSIDE",
                    "positive control: host junction creation/traversal at the probe location failed")
            os.rmdir(host_junction)  # removes the junction itself, never the target
            indir = sub / "in-task-dir"
            indir.mkdir()
            (indir / "inside.txt").write_text("IN_TASK_JUNCTION", encoding="ascii")
            probe_write = sub / "worker-write-positive.txt"
            code, _, err = await h.sandbox(ctx, task, f'echo OK>{_p(probe_write)}')
            _expect(code == 0 and probe_write.is_file(),
                    f"positive control: worker write in probe dir failed (exit={code}; {_tail(err)})")
            # Informational, as in group E: whether the profile allows junction creation at all.
            injunction = sub / "in-task-junction"
            code, _, _ = await h.sandbox(ctx, task, f'mklink /J {_p(injunction)} {_p(indir)}')
            details.append(f"in-task mklink /J exit={code} created={os.path.lexists(injunction)}")
            for label, target, secret in (("outside", outside, "SYNTHETIC-JUNCTION-OUTSIDE"),
                                          ("sibling", sibling, "SYNTHETIC-JUNCTION-SIBLING")):
                link = sub / f"created-junction-{label}"
                code, _, _ = await h.sandbox(ctx, task, f'mklink /J {_p(link)} {_p(target)}')
                created = os.path.lexists(link)
                details.append(f"{label}: mklink /J exit={code} created={created}; {_tail(h.last_err, 80)}")
                if not created:
                    ok = _denied(code) and ok
                    continue
                code, out, err = await h.sandbox(ctx, task, f'type {_p(link / "secret.txt")}')
                ok = _denied(code) and secret not in out + err and ok
                details.append(f"read exit={code}")
                code, _, _ = await h.sandbox(ctx, task, f'echo BAD>{_p(link / "bad.txt")}')
                ok = _denied(code) and not (target / "bad.txt").exists() and ok
                details.append(f"write exit={code}")
            await h.sandbox_control(ctx, task, "F2")
        finally:
            h.close(ctx)
        ok = (ok and (outside / "secret.txt").read_text() == "SYNTHETIC-JUNCTION-OUTSIDE"
              and (sibling / "secret.txt").read_text() == "SYNTHETIC-JUNCTION-SIBLING")
        rec("worker_junction_contained", PASS if ok else FAIL, "; ".join(details))

    await group(("worker_junction_contained",), worker_junction)
    return results


def _clear_git_readonly(root: Path) -> None:
    """Git writes its object files read-only and the no-follow remover never chmods. Only the harness's
    own repository metadata is touched, after every sandboxed process has drained; reparse points are
    skipped (never followed) and only plain, singly linked regular files are made writable."""
    if not os.path.isdir(root) or paths._is_link(os.stat(root, follow_symlinks=False)):
        return
    stack = [root]
    while stack:
        with os.scandir(stack.pop()) as entries:
            for entry in entries:
                st = os.stat(entry.path, follow_symlinks=False)
                if paths._is_link(st):
                    continue
                if stat_mod.S_ISDIR(st.st_mode):
                    stack.append(Path(entry.path))
                elif (stat_mod.S_ISREG(st.st_mode) and st.st_nlink == 1
                      and getattr(st, "st_file_attributes", 0) & stat_mod.FILE_ATTRIBUTE_READONLY):
                    os.chmod(entry.path, stat_mod.S_IWRITE | stat_mod.S_IREAD)


def _cleanup(h: _Harness | None, scratch: Path | None) -> str:
    if scratch is None:
        return "not started"
    if h is not None:
        from orchestrator.gitops import git

        if h.retained or any(ctx in h.rt._HELD for ctx in h.contexts):
            return f"retained (owned runtime leases uncertain): {scratch}"
        for wt in h.worktrees:
            git.forget_worktree(wt)
    from orchestrator.security import sandbox

    try:
        _clear_git_readonly(scratch / "repo" / ".git")
        sandbox.remove_tree(scratch)
    except (OSError, paths.PathPolicyError) as exc:
        where = getattr(exc, "filename", None) or ""
        return f"failed ({exc.__class__.__name__} at {where}); scratch retained: {scratch}"
    return "removed"


async def attest(data_dir: str | os.PathLike[str], *, adapter=None) -> AttestationResult:
    """Run every native check against the REAL installed Codex; record only on a complete PASS.

    Model-free: only `codex --version`, `codex sandbox`, `codex mcp list` and `codex debug
    prompt-input` are launched, with an owned CODEX_HOME and synthetic auth. Windows only.
    """
    from orchestrator.providers.codex import CodexAdapter
    from orchestrator.providers.process import check_recursion_guard
    from orchestrator.security import codex_runtime as rt

    result = AttestationResult(recorded=False)
    data = Path(os.path.abspath(os.fspath(data_dir)))
    if sys.platform != "win32":
        result.error = "native attestation requires Windows"
        return result
    check_recursion_guard(dict(os.environ))
    if not data.is_dir() or acl.check_private(data):
        result.error = f"data dir must exist and be owner-only: {data}"
        return result
    adapter = adapter or CodexAdapter(data_dir=data)
    try:
        exe = adapter.resolve()
        identity = await BinaryIdentityCache().current(exe, adapter.child_env())
        result.code_identity_sha256 = code_identity()
    except Exception as exc:  # noqa: BLE001 - nothing launched yet; report and record nothing
        result.error = f"binary identity unavailable: {exc}"[:300]
        return result
    result.codex_version, result.executable_sha256 = identity.version, identity.executable_sha256

    if len(str(data)) > MAX_DATA_DIR_CHARS:
        # Observed on Codex 0.169.0: with a scratch path of ~170 characters the trusted MCP positive control
        # never starts (owned home/profile paths approach MAX_PATH). Fail closed instead of a broken harness.
        result.error = f"data dir path longer than {MAX_DATA_DIR_CHARS} characters; native checks unsupported"
        return result
    scratch: Path | None = None
    harness: _Harness | None = None
    try:
        scratch = data / f"ca-{secrets.token_hex(4)}"
        with paths.directory_guard(data):
            rt._WindowsIO().directory(scratch, exclusive=True)
        harness = _Harness(scratch, adapter, tuple(exe.argv_prefix))
        harness.setup_repo()
        results = await _run_checks(harness)
    except Exception as exc:  # noqa: BLE001 - includes ContainmentError: record nothing
        result.error = f"attestation harness failed: {exc.__class__.__name__}: {exc}"[:300]
        results = {}
    finally:
        result.cleanup = _cleanup(harness, scratch)
    if harness is not None and harness.retained and result.error is None:
        result.error = "owned runtime cleanup uncertain; nothing recorded"
    result.checks = [results.get(name) or CheckResult(name, ERROR, "not executed") for name in REQUIRED_CHECKS]
    if result.error is None and all(c.result == PASS for c in result.checks):
        # Re-establish the binary identity: the binary must not have changed while the checks ran.
        try:
            after = await BinaryIdentityCache().current(exe, adapter.child_env())
        except Exception as exc:  # noqa: BLE001
            result.error = f"binary identity unavailable after checks: {exc}"[:300]
            return result
        if (after.version, after.executable_sha256) != (identity.version, identity.executable_sha256):
            result.error = "executable changed during attestation; nothing recorded"
            return result
        record = {
            "schema_version": SCHEMA_VERSION, "codex_version": identity.version,
            "executable_sha256": identity.executable_sha256, "executable_path": str(exe.path)[:4096],
            "code_identity_sha256": result.code_identity_sha256,
            "checks": [{"name": c.name, "result": PASS} for c in result.checks],
            "attested_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        result.record_path = str(write_record(data, record))
        result.recorded = True
    return result


def status_summary(data_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Record-only view for diagnostics (no binary inspection)."""
    record, reason = read_record(data_dir)
    if record is None:
        return {"record": False, "reason": reason}
    return {"record": True, "codex_version": record["codex_version"], "attested_at": record["attested_at"],
            "code_matches": _safe_code_identity() == record["code_identity_sha256"]}


def _safe_code_identity() -> str | None:
    try:
        return code_identity()
    except ActivationUnavailable:
        return None


_load_code_identity()
