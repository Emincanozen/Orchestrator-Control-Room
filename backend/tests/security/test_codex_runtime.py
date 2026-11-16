"""Pure runtime tests. All credentials, paths, handles and ACLs are fabricated.

No source auth, native filesystem/ACL operation, provider, subprocess or global
profile is used. Native sharing/MXC/startup/race proofs belong to integration.
"""

from __future__ import annotations

import ctypes
import gc
import json
import ntpath
import os
import threading
import tomllib
import weakref
from contextlib import contextmanager
from pathlib import Path

import pytest

from orchestrator.security import codex_runtime as rt

DATA = Path(r"C:\private-data\orchestrator")
CWD = r"C:\tasks\one"
EXTRA = r"D:\authorized\extra"
INSTALL = r"C:\installation"


def auth(account="synthetic-account", access="synthetic-access"):
    return json.dumps({
        "auth_mode": "chatgpt", "OPENAI_API_KEY": None,
        "tokens": {"access_token": access, "refresh_token": "synthetic-refresh",
                   "id_token": "synthetic-id", "account_id": account},
        "last_refresh": "2026-11-14T00:00:00Z",
    }).encode()


def key(path):
    return rt._win_path(str(path))


class FakeLease:
    def __init__(self, io, path):
        self.io, self.path = io, key(path)
        self.identity = io.files[self.path][0]
        self.handle = True
        io.leases.add(self)

    def read(self, limit):
        self.io.tick("read:" + ntpath.basename(self.path))
        data = self.io.files[self.path][1]
        if len(data) > limit:
            raise rt.CodexRuntimeUnavailable("fabricated oversized file")
        return data

    def write(self, data):
        self.io.tick("write:" + ntpath.basename(self.path))
        self.io.files[self.path] = (self.identity, data)
        self.io.tick("written:" + ntpath.basename(self.path))

    def close(self):
        if self.handle:
            self.io.tick("release:" + ntpath.basename(self.path))
            self.handle = None
            self.io.leases.discard(self)
            if self.path.endswith("runtime.lock"):
                self.io.locks.discard(self.path)


class FakeIO:
    def __init__(self):
        self.dirs = {key(DATA), key(CWD), key(EXTRA)}
        self.files = {}
        self.guarded = {}
        self.leases = set()
        self.locks = set()
        self.broad = set()
        self.reparse = set()
        self.bad_files = set()
        self.log = []
        self.next_identity = 0
        self.hook = lambda stage: None

    def tick(self, stage):
        self.log.append(stage)
        self.hook(stage)

    @contextmanager
    def guard(self, path):
        p = key(path)
        self.tick("guard:" + p)
        if p not in self.dirs or p in self.reparse:
            raise rt.CodexRuntimeUnavailable("fabricated bad directory")
        self.guarded[p] = self.guarded.get(p, 0) + 1
        try:
            yield str(path)
        finally:
            self.tick("unguard:" + p)
            self.guarded[p] -= 1

    def private(self, path):
        self.tick("private:" + key(path))
        if key(path) in self.broad:
            raise rt.CodexRuntimeUnavailable("fabricated broad ACL")

    def directory(self, path, *, exclusive=False):
        p = key(path)
        self.tick("directory:" + ntpath.basename(p))
        assert self.guarded.get(ntpath.dirname(p)), "unguarded creation"
        if exclusive and p in self.dirs:
            raise rt.CodexRuntimeUnavailable("fabricated existing directory")
        self.dirs.add(p)

    def exists(self, path):
        return key(path) in self.files

    def unsettled(self, known=()):
        # This shared fake kernel has no fallible low-level handle adoption. Other
        # contexts' active leases are checked by the fixture, not this context.
        return False

    def seed(self, path, content):
        self.next_identity += 1
        self.files[key(path)] = (self.next_identity, content)

    def lock(self, path):
        p = key(path)
        self.tick("lock")
        if p in self.locks or p in self.bad_files:
            raise rt.CodexRuntimeUnavailable("fabricated unsafe or busy lock")
        self.locks.add(p)
        if p not in self.files:
            self.seed(path, b"")
        return FakeLease(self, path)

    def create(self, path):
        self.tick("create:" + ntpath.basename(str(path)))
        assert self.guarded.get(key(path.parent)), "unguarded file creation"
        if key(path) in self.files:
            raise rt.CodexRuntimeUnavailable("fabricated stale file")
        self.seed(path, b"")
        return FakeLease(self, path)

    def pin(self, path):
        self.tick("pin:" + ntpath.basename(str(path)))
        if key(path) in self.bad_files:
            raise rt.CodexRuntimeUnavailable("fabricated file link")
        return FakeLease(self, path)

    def remove_owned(self, path, identity):
        self.tick("remove:auth.json")
        assert self.guarded.get(key(path.parent)), "unguarded cleanup"
        if key(path) in self.bad_files or self.files[key(path)][0] != identity:
            raise rt.CodexRuntimeUnavailable("fabricated cleanup mismatch")
        del self.files[key(path)]


@pytest.fixture
def io(monkeypatch):
    fake = FakeIO()
    monkeypatch.setattr(rt, "_WindowsIO", lambda: fake)
    monkeypatch.setattr(rt, "_install_roots", lambda: (key(INSTALL),))
    monkeypatch.setattr(rt, "_assert_plain_roots", lambda roots: None)
    yield fake
    # Tests settle synthetic retained contexts explicitly; no destructor recovery.
    assert not fake.leases
    assert not any(fake.guarded.values())


def context(**kwargs):
    values = {"data_dir": DATA, "cwd": CWD, "extra_dirs": (), "child_env": {"PATH": "synthetic-path"}}
    values.update(kwargs)
    return rt.CodexRunContext(**values)


def test_import_is_from_selected_worktree():
    expected = Path(__file__).absolute().parents[2] / "orchestrator" / "security" / "codex_runtime.py"
    assert Path(rt.__file__).absolute() == expected


def test_launch_rechecks_all_roots_and_failed_check_vetoes_reuse(io, monkeypatch):
    ctx = context(extra_dirs=(EXTRA,)).prepare(auth())
    checked = []

    def validate(roots):
        checked.append(roots)
        if len(checked) == 2:
            raise rt.CodexRuntimeUnavailable("synthetic late alias")

    monkeypatch.setattr(rt, "_assert_plain_roots", validate)
    try:
        ctx.validate_before_launch()
        with pytest.raises(rt.CodexRuntimeUnavailable, match="synthetic late alias"):
            ctx.validate_before_launch()
        assert checked == [(key(CWD), (key(EXTRA))), (key(CWD), (key(EXTRA)))]
        # Even if a later scan would succeed, this invocation remains vetoed.
        with pytest.raises(rt.CodexRuntimeCancelled):
            ctx.validate_before_launch()
        with pytest.raises(rt.CodexRuntimeCancelled):
            _ = ctx.environment
        assert io.leases, "failure must not release leases before provider drain"
    finally:
        ctx.close()


@pytest.mark.parametrize("stage", ["new", "closed", "retained", "cancelled"])
def test_launch_requires_ready_uncancelled_context(io, monkeypatch, stage):
    ctx = context()
    if stage != "new":
        ctx.prepare(auth())
    if stage == "closed":
        ctx.close()
    elif stage == "retained":
        ctx.close(retain=True)
    elif stage == "cancelled":
        ctx.cancel_pending_start()
    monkeypatch.setattr(rt, "_assert_plain_roots", lambda roots: pytest.fail("invalid launch scanned roots"))
    try:
        with pytest.raises(rt.CodexRuntimeError):
            ctx.validate_before_launch()
    finally:
        ctx.close()


def test_cancellation_during_launch_scan_prevents_launch_and_keeps_leases(io, monkeypatch):
    ctx = context().prepare(auth())
    monkeypatch.setattr(rt, "_assert_plain_roots", lambda roots: ctx.cancel_pending_start())
    try:
        with pytest.raises(rt.CodexRuntimeCancelled):
            ctx.validate_before_launch()
        assert io.leases
        with pytest.raises(rt.CodexRuntimeCancelled):
            _ = ctx.environment
    finally:
        ctx.close()


def test_config_scopes_are_fixed_bounded_and_deny_network(io):
    ctx = context(extra_dirs=(EXTRA,)).prepare(auth())
    raw = io.files[key(ctx.config_path)][1]
    cfg = tomllib.loads(raw.decode())
    assert cfg["windows"] == {"sandbox": "mxc"}
    assert cfg["features"] == {"hooks": False, "plugins": False, "apps": False, "multi_agent": False}
    assert cfg["approval_policy"] == "never"
    assert cfg["cli_auth_credentials_store"] == "file"
    assert cfg["web_search"] == "disabled"
    assert cfg["mcp_servers"] == {}
    assert cfg["default_permissions"] == "orch-read"
    for name, mode in (("orch-read", "read"), ("orch-write", "write")):
        profile = cfg["profiles"][name]
        assert profile == {"default_permissions": name, "approval_policy": "never", "web_search": "disabled"}
        permission = cfg["permissions"][name]
        assert permission["network"] == {"enabled": False}
        fs = permission["filesystem"]
        assert fs[":minimal"] == "read"
        for scope in (":workspace_roots", key(EXTRA)):
            assert fs[scope] == {".": mode, ".git": "read", ".codex": "read", ".agents": "read", ".aws": "read"}
        assert fs[str(ctx.home / "helpers")] == fs[str(ctx.home / "profile")] == "read"
        assert fs[ctx.environment["TEMP"]] == "write"
        assert {p for p, value in fs.items() if value == "write"} == {ctx.environment["TEMP"]}
        assert str(ctx.home) not in fs
    for path in (CWD, EXTRA):
        p = rt.PureWindowsPath(key(path))
        for parent in (p, *p.parents):
            assert cfg["projects"][str(parent)] == {"trust_level": "untrusted"}
    assert raw == rt._render_config(key(CWD), (key(EXTRA),), ctx.environment["TEMP"],
                                    (str(ctx.home / "helpers"), str(ctx.home / "profile")))
    assert b"synthetic-access" not in raw
    ctx.close()


def test_home_identity_canonicalizes_roots_and_account_rotation(io):
    one = context(extra_dirs=(EXTRA, EXTRA, CWD)).prepare(auth())
    home = one.home
    one.close()
    two = context(cwd="c:/TASKS/one/", extra_dirs=(EXTRA.lower(),)).prepare(auth(access="rotated-synthetic"))
    assert two.home == home
    two.close()
    three = context(extra_dirs=(EXTRA,)).prepare(auth(account="another-synthetic-account"))
    assert three.home != home
    three.close()
    four = context().prepare(auth())
    assert four.home != home
    four.close()
    io.dirs.add(key(r"C:\tasks\two"))
    five = context(cwd=r"C:\tasks\two", extra_dirs=(EXTRA,)).prepare(auth())
    assert five.home != home
    five.close()


def test_missing_account_binds_exact_snapshot_digest():
    first, second = auth(account=None), auth(account=None, access="other-synthetic")
    assert rt._auth_identity(first).startswith("snapshot:")
    assert rt._home_key(key(CWD), (), rt._auth_identity(first)) != rt._home_key(key(CWD), (), rt._auth_identity(second))
    assert rt._home_key(key(CWD), (key(EXTRA),), "account:x") != rt._home_key(key(CWD), (), "account:x")


@pytest.mark.parametrize("raw", [
    b"", b"{secret-malformed", b"\xff", b"[]", b"null", b'{"auth_mode":"api_key"}',
    b'{"auth_mode":"unknown"}', b'{"auth_mode":"chatgpt","tokens":{}}',
    b'{"auth_mode":"chatgpt","auth_mode":"chatgpt"}', b'{"tokens":NaN}',
    b"[" * 2000 + b"]" * 2000, b" " * (rt.AUTH_LIMIT + 1), bytearray(b"{}"),
])
def test_invalid_auth_is_generic_without_parser_context(raw):
    with pytest.raises(rt.CodexRuntimeUnavailable) as error:
        rt._auth_identity(raw)
    assert error.value.__context__ is None
    assert error.value.__cause__ is None
    assert str(error.value) == "unsupported subscription authentication snapshot"


@pytest.mark.parametrize("change", [
    {"OPENAI_API_KEY": "synthetic-api-key"}, {"api_key": "synthetic-api-key"},
    {"auth_mode": "chatgptAuthTokens"}, {"auth_mode": None}, {"arbitrary": "synthetic-secret"},
    {"tokens": {"access_token": "synthetic"}}, {"last_refresh": {}},
])
def test_auth_rejects_keys_unknown_modes_and_fields(change):
    obj = json.loads(auth())
    obj.update(change)
    with pytest.raises(rt.CodexRuntimeUnavailable):
        rt._auth_identity(json.dumps(obj).encode())


def test_auth_limit_is_inclusive_and_nested_duplicate_rejected():
    sample = auth()
    assert rt._auth_identity(sample + b" " * (rt.AUTH_LIMIT - len(sample)))
    duplicate = sample.replace(b'"access_token": "synthetic-access"',
                               b'"access_token":"one", "access_token":"two"')
    with pytest.raises(rt.CodexRuntimeUnavailable):
        rt._auth_identity(duplicate)


@pytest.mark.parametrize("path", [
    "", "relative", "C:relative", "C:\\", r"\\server\share", r"\\?\C:\data", r"C:\data:stream",
    r"C:\data\..\private", r"C:\data\.\private", "C:\\data\\bad ", "C:\\data\\bad.",
    r"C:\data\NUL.txt", r"C:\data\COM1", r"C:\PROGRA~1\data", "C:\\data\\bad\x00", "C:\\data\\bad\n",
])
def test_ambiguous_paths_are_rejected(path):
    with pytest.raises(rt.CodexRuntimeUnavailable):
        context(cwd=path)


@pytest.mark.parametrize("data", [r"C:\tasks", r"C:\tasks\one\data", r"C:\installation\data", r"C:\private\Temp\data"])
def test_unsafe_layout_refused_before_any_writes(io, data):
    with pytest.raises(rt.CodexRuntimeUnavailable):
        context(data_dir=Path(data)).prepare(auth())
    assert not io.files
    assert not any(stage.startswith("directory:") for stage in io.log)


@pytest.mark.parametrize("extra", [r"C:\tasks", r"C:\tasks\one\child", r"C:\installation\sub"])
def test_overlapping_additional_roots_are_explicitly_unsupported(io, extra):
    with pytest.raises(rt.CodexRuntimeUnavailable):
        context(extra_dirs=(extra,)).prepare(auth())
    assert not io.files


def test_temp_input_and_installation_cwd_refused_before_writes(io):
    with pytest.raises(rt.CodexRuntimeUnavailable):
        context(child_env={"TEMP": str(DATA)}).prepare(auth())
    with pytest.raises(rt.CodexRuntimeUnavailable):
        context(cwd=INSTALL).prepare(auth())
    assert not io.files


def test_existing_broad_root_or_reparse_rejected_without_repair(io):
    io.broad.add(key(DATA))
    with pytest.raises(rt.CodexRuntimeUnavailable):
        context().prepare(auth())
    assert not io.files
    io.broad.clear()
    io.reparse.add(key(DATA))
    with pytest.raises(rt.CodexRuntimeUnavailable):
        context().prepare(auth())
    assert not io.files


def test_environment_copies_and_overrides_all_aliases_without_global_mutation(io):
    original = dict(os.environ)
    source = {"Path": "sanitized-path", "OPENAI_API_KEY": "synthetic-secret", "Codex_Home": "ignored",
              "UserProfile": "ignored", "home": "ignored", "AppData": "ignored", "LocalAppData": "ignored",
              "ORCHESTRATOR_TOKEN": "synthetic-secret", "CODEX_AUTH_TOKEN": "synthetic-secret",
              "CLAUDE_CONFIG_DIR": "synthetic-pointer", "CODEX_CONFIG": "synthetic-pointer",
              "GEMINI_API_KEY": "synthetic-secret", "NODE_OPTIONS": "synthetic-pointer",
              "PYTHONPATH": "synthetic-pointer", "NPM_CONFIG_USERCONFIG": "synthetic-pointer",
              "TEMP": r"C:\old-temp", "TMP": r"C:\old-temp", "AI_ORCHESTRATOR_DEPTH": "1"}
    before = dict(source)
    ctx = context(child_env=source)
    source["Path"] = "later-change"
    ctx.prepare(auth())
    env = ctx.environment
    assert env["PATH"] == "sanitized-path"
    assert env["AI_ORCHESTRATOR_DEPTH"] == "1"
    assert not {"OPENAI_API_KEY", "CODEX_AUTH_TOKEN", "ORCHESTRATOR_TOKEN"} & env.keys()
    assert all(k == k.upper() for k in env)
    assert len({k.upper() for k in env}) == len(env)
    assert not {"CLAUDE_CONFIG_DIR", "CODEX_CONFIG", "GEMINI_API_KEY", "NODE_OPTIONS", "PYTHONPATH",
                "NPM_CONFIG_USERCONFIG"} & env.keys()
    for alias in ("CODEX_HOME", "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"):
        assert rt._within(key(env[alias]), key(ctx.home))
    assert env["TEMP"] == env["TMP"]
    assert env["CODEX_HOME"] == str(ctx.home)
    env["HOME"] = "caller-mutation"
    assert ctx.environment["HOME"] != "caller-mutation"
    assert dict(os.environ) == original
    assert all(source[k] == v for k, v in before.items() if k != "Path")
    assert "synthetic-access" not in repr(ctx)
    assert "synthetic-access" not in repr(vars(ctx))
    assert auth() not in vars(ctx).values()
    ctx.close()
    with pytest.raises(rt.CodexRuntimeUnavailable):
        _ = ctx.environment


@pytest.mark.parametrize("env", [{"PATH": "a", "Path": "b"}, {"X": "a\x00b"}, {"=X": "x"}, {"X": 2}])
def test_invalid_environment_refused(env):
    with pytest.raises(rt.CodexRuntimeUnavailable):
        context(child_env=env)


def test_lock_serializes_same_home_and_config_pin_lasts_through_drain(io):
    first = context().prepare(auth())
    lock, config = first._lock, first._config
    with pytest.raises(rt.CodexRuntimeUnavailable):
        context().prepare(auth())
    assert lock.handle and config.handle
    assert key(first.home / "auth.json") in io.files
    assert not any(stage == "write:runtime.lock" for stage in io.log)
    first.close()
    second = context().prepare(auth())
    assert second.home == first.home
    second.close()


def test_config_mismatch_and_stale_auth_are_never_overwritten_or_adopted(io):
    first = context().prepare(auth())
    cfg, owned_auth = first.config_path, first.home / "auth.json"
    first.close()
    io.seed(cfg, b"mismatch-synthetic")
    with pytest.raises(rt.CodexRuntimeUnavailable):
        context().prepare(auth())
    assert io.files[key(cfg)][1] == b"mismatch-synthetic"
    io.seed(cfg, rt._render_config(key(CWD), (), str(first.home / "command-scratch"),
                                 (str(first.home / "helpers"), str(first.home / "profile"))))
    io.seed(owned_auth, b"crash-left-synthetic")
    with pytest.raises(rt.CodexRuntimeUnavailable):
        context().prepare(auth())
    assert io.files[key(owned_auth)][1] == b"crash-left-synthetic"


@pytest.mark.parametrize("stage", ["lock", "create:config.toml", "write:config.toml", "pin:config.toml",
                                  "create:auth.json", "write:auth.json", "written:auth.json", "pin:auth.json"])
def test_cancel_at_preparation_boundaries_is_joined_and_cleans_only_owned_auth(io, stage):
    ctx = context()
    io.hook = lambda observed: ctx.cancel_pending_start() if observed == stage else None
    with pytest.raises(rt.CodexRuntimeCancelled):
        ctx.prepare(auth())
    assert not any(path.endswith("auth.json") for path in io.files)
    assert ctx not in rt._HELD
    assert not io.leases


def test_pre_cancel_prevents_io_and_reprepare_is_rejected(io):
    ctx = context()
    ctx.cancel_pending_start()
    with pytest.raises(rt.CodexRuntimeCancelled):
        ctx.prepare(auth())
    assert not io.log
    with pytest.raises(rt.CodexRuntimeUnavailable):
        ctx.prepare(auth())
    ctx.close()


def test_cancel_after_prepare_vetoes_environment_until_settled_cleanup(io):
    ctx = context().prepare(auth())
    ctx.cancel_pending_start()
    with pytest.raises(rt.CodexRuntimeCancelled):
        _ = ctx.environment
    assert ctx._auth.handle
    ctx.close()


def test_cancel_never_waits_for_preparation_mutex_and_close_joins(io):
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    ctx = context()
    errors = []

    def hook(stage):
        if stage == "create:auth.json":
            entered.set()
            assert release.wait(5)

    def prepare():
        try:
            ctx.prepare(auth())
        except rt.CodexRuntimeCancelled:
            errors.append("cancelled")

    def close():
        ctx.close()
        closed.set()

    io.hook = hook
    worker = threading.Thread(target=prepare)
    closer = threading.Thread(target=close)
    worker.start()
    try:
        assert entered.wait(5)
        ctx.cancel_pending_start()  # would deadlock here if it acquired preparation's mutex
        closer.start()
        assert not closed.wait(0.02)
    finally:
        release.set()
        worker.join(5)
        if closer.ident is not None:
            closer.join(5)
    assert not worker.is_alive() and not closer.is_alive()
    assert errors == ["cancelled"] and closed.is_set()
    assert ctx not in rt._HELD


def test_unknown_exit_keeps_strong_references_without_destructor(io):
    ctx = context().prepare(auth())
    auth_path = key(ctx.home / "auth.json")
    ctx.close(retain=True)
    ref = weakref.ref(ctx)
    del ctx
    gc.collect()
    retained = ref()
    assert retained in rt._HELD
    assert auth_path in io.files and retained._config.handle and retained._lock.handle
    with pytest.raises(rt.CodexRuntimeUnavailable):
        _ = retained.environment
    retained.close()  # test explicitly declares the fabricated job drained
    assert auth_path not in io.files and retained not in rt._HELD
    assert "__del__" not in rt.CodexRunContext.__dict__


def test_cleanup_replacement_retains_leases_and_never_deletes_foreign_auth(io):
    ctx = context().prepare(auth())
    path = ctx.home / "auth.json"
    identity, original = io.files[key(path)]
    io.seed(path, b"foreign-synthetic")
    with pytest.raises(rt.CodexRuntimeUnavailable, match="leases retained"):
        ctx.close()
    assert io.files[key(path)][1] == b"foreign-synthetic"
    assert ctx in rt._HELD and ctx._config.handle and ctx._lock.handle
    io.files[key(path)] = identity, original
    ctx.close()


def test_write_failure_removes_partial_auth_and_sanitizes_underlying_error(io):
    ctx = context()

    def fail(stage):
        if stage == "written:auth.json":
            raise RuntimeError("synthetic-secret-do-not-expose")

    io.hook = fail
    with pytest.raises(rt.CodexRuntimeUnavailable) as error:
        ctx.prepare(auth())
    assert str(error.value) == "owned Codex preparation refused"
    assert error.value.__context__ is None
    assert "synthetic-secret" not in repr(error.value)
    assert not any(p.endswith("auth.json") for p in io.files)


def test_cleanup_uncertainty_during_prepare_is_reported_and_kept_referenced(io):
    ctx = context()

    def fail(stage):
        if stage == "written:auth.json":
            raise RuntimeError("synthetic-write-error")
        if stage == "remove:auth.json":
            raise RuntimeError("synthetic-cleanup-error")

    io.hook = fail
    with pytest.raises(rt.CodexRuntimeUnavailable, match="cleanup uncertain; leases retained"):
        ctx.prepare(auth())
    assert ctx in rt._HELD and ctx._config.handle and ctx._lock.handle
    io.hook = lambda _: None
    ctx.close()


def test_unadopted_native_lease_blocks_release_of_known_leases(io, monkeypatch):
    ctx = context().prepare(auth())
    monkeypatch.setattr(io, "unsettled", lambda known=(): True)
    with pytest.raises(rt.CodexRuntimeUnavailable, match="leases retained"):
        ctx.close()
    assert ctx in rt._HELD and ctx._auth.handle and ctx._config.handle and ctx._lock.handle
    assert key(ctx.home / "auth.json") in io.files
    monkeypatch.setattr(io, "unsettled", lambda known=(): False)
    ctx.close()


def test_changed_new_config_is_rejected_before_auth_copy(io):
    def replace(stage):
        if stage == "pin:config.toml":
            path = next(p for p in io.files if p.endswith("config.toml"))
            io.seed(path, io.files[path][1])  # same bytes, different opened identity

    io.hook = replace
    with pytest.raises(rt.CodexRuntimeUnavailable):
        context().prepare(auth())
    assert "create:auth.json" not in io.log


def test_prepare_failure_does_not_keep_auth_in_its_traceback_frame(io):
    io.hook = lambda stage: (_ for _ in ()).throw(RuntimeError("synthetic")) if stage == "lock" else None
    with pytest.raises(rt.CodexRuntimeUnavailable) as error:
        context().prepare(auth())
    tb = error.value.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_name == "prepare":
            assert tb.tb_frame.f_locals["auth_bytes"] == b""
        tb = tb.tb_next


@pytest.mark.parametrize("state", ["ready", "retained", "closed"])
def test_reprepare_clears_secret_argument_without_releasing_existing_leases(io, state):
    ctx = context().prepare(auth())
    if state == "retained":
        ctx.close(retain=True)
    elif state == "closed":
        ctx.close()
    before = set(io.leases)
    try:
        with pytest.raises(rt.CodexRuntimeUnavailable) as caught:
            ctx.prepare(auth(access="SYNTHETIC-REPREPARE-SECRET"))
        assert ctx._state == state and set(io.leases) == before
        tb = caught.value.__traceback__
        while tb:
            if tb.tb_frame.f_code.co_name == "prepare":
                assert tb.tb_frame.f_locals["auth_bytes"] == b""
            tb = tb.tb_next
    finally:
        ctx.close()


def test_interrupted_auth_write_does_not_propagate_credential_frames(io):
    ctx = context()

    def interrupt(stage):
        if stage == "write:auth.json":
            io.hook = lambda _: None
            raise KeyboardInterrupt()

    io.hook = interrupt
    with pytest.raises(KeyboardInterrupt) as caught:
        ctx.prepare(auth(access="SYNTHETIC-INTERRUPTED-SECRET"))
    assert ctx._state == "closed" and not io.leases
    assert caught.value.__context__ is None
    tb = caught.value.__traceback__
    while tb:
        if tb.tb_frame.f_code.co_name in {"prepare", "_create_and_pin", "write"}:
            assert all("SYNTHETIC-INTERRUPTED-SECRET" not in repr(value)
                       for value in tb.tb_frame.f_locals.values())
        tb = tb.tb_next


def test_non_windows_fails_closed_without_native_calls(monkeypatch):
    monkeypatch.setattr(rt.sys, "platform", "linux")
    with pytest.raises(rt.CodexRuntimeUnavailable, match="requires Windows"):
        rt._WindowsIO()


def test_native_lock_and_pin_use_required_flags_with_fake_kernel(monkeypatch):
    """Exercise argument and handle checks using a fake kernel, not Windows IO."""
    from ctypes import wintypes as w

    calls = []

    class Info(ctypes.Structure):
        _fields_ = [("dwFileAttributes", w.DWORD), ("nNumberOfLinks", w.DWORD),
                    ("dwVolumeSerialNumber", w.DWORD), ("nFileIndexHigh", w.DWORD), ("nFileIndexLow", w.DWORD)]

    class Kernel:
        attributes, links, final = 0, 1, r"\\?\C:\private-data\orchestrator\runtime.lock"

        def CreateFileW(self, *args):
            calls.append(args)
            return 123

        def GetFileInformationByHandle(self, handle, ptr):
            info = ctypes.cast(ptr, ctypes.POINTER(Info)).contents
            info.dwFileAttributes, info.nNumberOfLinks = self.attributes, self.links
            info.dwVolumeSerialNumber, info.nFileIndexLow = 7, 9
            return True

        def GetFinalPathNameByHandleW(self, handle, buffer, length, flags):
            buffer.value = self.final
            return len(self.final)

        def CloseHandle(self, handle):
            calls.append(("close", handle))
            return True

        def LocalFree(self, sd):
            pass

        def SetFileInformationByHandle(self, handle, info_class, ptr, size):
            calls.append(("delete", handle, info_class, size, ctypes.cast(ptr, ctypes.POINTER(ctypes.c_ubyte))[0]))
            return True

    native = rt._WindowsIO.__new__(rt._WindowsIO)
    native.c, native.w, native.k, native.leases = ctypes, w, Kernel(), set()
    monkeypatch.setattr(native, "_security", lambda directory: (ctypes.c_int(), None))
    monkeypatch.setattr(native, "private", lambda path: None)
    monkeypatch.setattr(rt.paths, "_BHFI", Info, raising=False)
    path = DATA / "runtime.lock"
    lock = native.lock(path)
    assert calls[0][1:3] == (0x80000000, 0)
    assert calls[0][4] == 4  # OPEN_ALWAYS, no truncate/write
    assert calls[0][5] & 0x00200000  # OPEN_REPARSE_POINT
    lock.close()
    native.pin(path).close()
    assert calls[2][1:3] == (0x80000000, 1)
    assert calls[2][4] == 3
    native.create(path).close()
    assert calls[4][4] == 1  # CREATE_NEW, never adopt auth
    before_delete = len(calls)
    native.remove_owned(path, (7, 0, 9))
    assert calls[before_delete][1:3] == (0x80010000, 0)  # DELETE|READ, share0
    assert calls[before_delete][4] == 3
    assert calls[before_delete + 1] == ("delete", 123, 4, 1, 1)
    before_mismatch = len(calls)
    with pytest.raises(rt.CodexRuntimeUnavailable):
        native.remove_owned(path, (1, 2, 3))
    assert not any(call[0] == "delete" for call in calls[before_mismatch:])
    for attributes, links, final in ((0x400, 1, native.k.final), (0x10, 1, native.k.final),
                                     (0, 2, native.k.final), (0, 0, native.k.final),
                                     (0, 1, r"\\?\C:\foreign\runtime.lock")):
        native.k.attributes, native.k.links, native.k.final = attributes, links, final
        with pytest.raises(rt.CodexRuntimeUnavailable):
            native.lock(path)
        assert not native.leases
