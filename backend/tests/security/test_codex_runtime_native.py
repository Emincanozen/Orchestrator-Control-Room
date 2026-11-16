"""Native owned-home proofs, synthetic auth only; no model/network generation.

The caller supplies a NEW private Orchestrator-owned data fixture outside install
and TEMP via ORCHESTRATOR_CODEX_NATIVE_TEST_ROOT. No original config or ACL repair.
"""

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

from orchestrator.gitops import git, worktrees
from orchestrator.providers.codex import CodexAdapter
from orchestrator.providers.process import run_capture
from orchestrator.security import acl, paths
from orchestrator.security import codex_runtime as rt

pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows owned runtime")


def _cmd_safe(path) -> bool:
    """True when `path` can be passed to `cmd /c` without quotes (no spaces or cmd metacharacters)."""
    return not any(c in str(path) for c in ' &|<>^%!"()')


def fake_auth():
    return json.dumps({"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": {
        "access_token": "SYNTHETIC-ACCESS", "refresh_token": "SYNTHETIC-REFRESH",
        "id_token": "SYNTHETIC-ID", "account_id": "synthetic-native-account"}}).encode()


@pytest.fixture
def native_context(tmp_path):
    value = os.environ.get("ORCHESTRATOR_CODEX_NATIVE_TEST_ROOT")
    if not value:
        pytest.skip("explicit private non-TEMP native data fixture required")
    parent = Path(value)
    assert parent.is_absolute() and not acl.check_private(parent)
    data = parent / ("case-" + uuid.uuid4().hex)
    io = rt._WindowsIO()
    with paths.directory_guard(parent):
        io.directory(data, exclusive=True)
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "README.md").write_text("synthetic native fixture\n")
    base = git.init_repo(repo)
    task = Path(worktrees.create(repo, tmp_path / "task-a", "native-a", base).path)
    sibling = Path(worktrees.create(repo, tmp_path / "task-b", "native-b", base).path)
    (sibling / "private.txt").write_text("SYNTHETIC-SIBLING")
    adapter = CodexAdapter()
    ctx = rt.CodexRunContext(data, str(task), (), adapter.child_env())
    try:
        yield ctx, adapter, task, sibling
    finally:
        # No model process exists in these tests; metadata helpers join/drain in run_capture.
        ctx.close()


def test_native_config_pin_lock_and_auth_cleanup(native_context):
    ctx, adapter, task, _ = native_context
    ctx.prepare(fake_auth())
    expected = ctx.config_path.read_bytes()
    with pytest.raises(OSError):
        ctx.config_path.write_bytes(b"foreign-config")
    with pytest.raises(OSError):
        ctx.config_path.rename(ctx.home / "renamed-config")
    other = rt.CodexRunContext(ctx._data, str(task), (), adapter.child_env())
    with pytest.raises(rt.CodexRuntimeUnavailable):
        other.prepare(fake_auth())
    assert ctx.config_path.read_bytes() == expected
    assert (ctx.home / "auth.json").read_bytes() == fake_auth()
    ctx.close()
    assert not (ctx.home / "auth.json").exists()
    assert ctx not in rt._HELD


def test_native_stale_auth_is_preserved_and_blocks_reuse(native_context):
    ctx, adapter, task, _ = native_context
    ctx.prepare(fake_auth())
    ctx.close()
    stale = ctx.home / "auth.json"
    stale.write_bytes(b"SYNTHETIC-STALE")
    other = rt.CodexRunContext(ctx._data, str(task), (), adapter.child_env())
    with pytest.raises(rt.CodexRuntimeUnavailable):
        other.prepare(fake_auth())
    assert stale.read_bytes() == b"SYNTHETIC-STALE"


@pytest.mark.parametrize("resume", [None, "synthetic-resume-session"])
async def test_native_owned_adapter_lifecycle_with_fake_provider(native_context, monkeypatch, resume):
    """Real native context + CLI inventory/job; fake model transport, NOT real coding."""
    from orchestrator.providers import codex as codex_module
    from orchestrator.providers.base import AgentEventKind, ExecutionRequest

    ctx, adapter, task, _ = native_context
    calls = []

    class FakeProvider:
        pid = None
        returncode = 0
        timed_out = False
        stdout_tail = stderr_tail = ""

        async def stream(self):
            yield "stdout", '{"type":"turn.completed"}'

        async def wait(self):
            return 0

        async def terminate_tree(self):
            calls.append("drain")
            return False  # False is not evidence of a failure; no fake child exists.

    async def spawn(argv, **kwargs):
        assert Path(kwargs["env"]["CODEX_HOME"]) == ctx.home
        assert (ctx.home / "auth.json").read_bytes() == fake_auth()
        assert kwargs["cwd"] == str(task)
        assert 'windows.sandbox="mxc"' in argv
        assert 'default_permissions="orch-write"' in argv
        calls.append("spawn")
        return FakeProvider()

    # Explicit test instance only; default production activation remains refused.
    adapter._require_activated_runtime = lambda: None
    adapter._new_owned_context = lambda request: ctx
    adapter._spawner = spawn
    monkeypatch.setattr(codex_module, "read_subscription_auth", lambda env: fake_auth())
    request = ExecutionRequest(run_id="native-owned", prompt="synthetic", cwd=str(task),
                               permission="workspace_write", resume_session_id=resume)
    events = [event async for event in adapter.execute(request)]
    assert calls == ["spawn", "drain"]
    assert events[-1].kind == AgentEventKind.RESULT and events[-1].data["success"]
    assert not (ctx.home / "auth.json").exists() and ctx not in rt._HELD
    assert not adapter._owned_contexts


async def test_native_owned_cancel_aborts_suspended_process_before_execution(native_context, monkeypatch):
    """Actual process/job cancellation through adapter; synthetic command, no model."""
    from orchestrator.providers import codex as codex_module
    from orchestrator.providers.base import ExecutionRequest
    from orchestrator.providers.process import CommandSpec, ResolvedExecutable

    ctx, adapter, task, _ = native_context
    created, release = asyncio.Event(), asyncio.Event()
    real_create = asyncio.create_subprocess_exec
    marker = task / "cancelled-process-must-not-run.txt"
    child_pids = []

    async def delayed_create(*args, **kwargs):
        child = await real_create(*args, **kwargs)
        child_pids.append(child.pid)
        created.set()
        await release.wait()
        return child

    async def empty_inventory(*args, **kwargs):
        return ()

    adapter._require_activated_runtime = lambda: None  # explicit synthetic test instance
    adapter._new_owned_context = lambda request: ctx
    adapter._mcp_inventory = empty_inventory
    adapter._resolver = lambda name: ResolvedExecutable((sys.executable,), sys.executable, "test")
    adapter._build_owned_command = lambda request, context: CommandSpec(
        args=["-c", f"open({str(marker)!r}, 'w').write('unexpected')"],
        stdin_text=None, environment=context.environment)
    monkeypatch.setattr(codex_module, "read_subscription_auth", lambda env: fake_auth())
    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_create)
    request = ExecutionRequest(run_id="cancel-native-owned", prompt="synthetic", cwd=str(task))
    stream = adapter.execute(request)
    pending = asyncio.create_task(anext(stream))
    try:
        await asyncio.wait_for(created.wait(), 10)
        assert await adapter.cancel(request.run_id)
        release.set()
        events = [await pending, *[event async for event in stream]]
        assert events[-1].data["error_category"] == "cancelled"
        assert child_pids and not marker.exists()
        assert not adapter._spawns and not adapter._procs
        assert not (ctx.home / "auth.json").exists()
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await stream.aclose()


async def test_native_named_profiles_deny_sibling_auth_and_protected_write(native_context):
    ctx, adapter, task, sibling = native_context
    ctx.prepare(fake_auth())
    canary = ctx.home / "fake-private.txt"
    canary.write_text("SYNTHETIC-PRIVATE")
    prefix = adapter.resolve().argv_prefix
    system_cmd = str(Path(os.environ["SYSTEMROOT"]) / "System32" / "cmd.exe")

    async def run(command, profile="orch-write"):
        return await run_capture([*prefix, "--disable", "hooks", "--disable", "plugins", "--disable", "apps",
            "sandbox", "--include-managed-config", "-P", profile, "-C", str(task), "--",
            system_cmd, "/d", "/c", command], env=ctx.environment, cwd=str(task), timeout=30)

    code, _, _ = await run("echo ORCH_NATIVE_READY")
    assert code == 0, "native profile startup failed"
    code, _, _ = await run("echo ORCH_NATIVE_WRITE>answer.txt")
    assert code == 0 and (task / "answer.txt").read_text().strip() == "ORCH_NATIVE_WRITE"
    # Absolute paths, unquoted: argv quoting escapes inner quotes as \" which cmd.exe rejects as a syntax
    # error, making a denial vacuous. Positive controls use the same absolute-path syntax.
    for p in (canary, sibling, ctx.home, task):
        assert _cmd_safe(p), f"fixture path unusable unquoted: {p}"
    code, out, _ = await run(f'type {task / "README.md"}')
    assert code == 0 and "synthetic native fixture" in out, "absolute in-task read positive control failed"
    code, _, _ = await run(f'echo OK>{task / "abs-positive.txt"}')
    assert code == 0 and (task / "abs-positive.txt").exists(), "absolute in-task write positive control failed"
    for command in (f'type {canary}', f'type {sibling / "private.txt"}',
                    f'echo BAD>{sibling / "must-not-write.txt"}',
                    f'echo BAD>{ctx.home / "must-not-write.txt"}'):
        code, _, _ = await run(command)
        assert code != 0, "native protected-path attack unexpectedly succeeded"
    code, _, _ = await run("echo BAD>readonly-must-not-write.txt", "orch-read")
    assert code != 0
    assert not (task / "readonly-must-not-write.txt").exists()
    assert not (sibling / "must-not-write.txt").exists()
    assert not (ctx.home / "must-not-write.txt").exists()
    assert canary.read_text() == "SYNTHETIC-PRIVATE"
    assert (sibling / "private.txt").read_text() == "SYNTHETIC-SIBLING"


async def test_native_late_project_mcp_is_ignored_with_trusted_positive_control(native_context):
    ctx, adapter, task, _ = native_context
    ctx.prepare(fake_auth())
    environment = ctx.environment
    prefix = adapter.resolve().argv_prefix
    assert await adapter._mcp_inventory(prefix, environment, cwd=str(task)) == ()
    marker = task.parent / "controlled-mcp-started"
    program = task.parent / "controlled_mcp.py"
    program.write_text('import pathlib,sys\npathlib.Path(sys.argv[1]).write_text("STARTED")\n')
    project_config = task / ".codex"
    project_config.mkdir()
    (project_config / "config.toml").write_text(
        '[mcp_servers."late.controlled"]\ncommand=' + json.dumps(sys.executable)
        + '\nargs=' + json.dumps([str(program), str(marker)]) + '\n', encoding="utf-8")
    argv = [*prefix, "--disable", "hooks", "--disable", "plugins", "--disable", "apps", "debug", "prompt-input"]
    code, _, _ = await run_capture(argv, env=environment, cwd=str(task), timeout=30)
    assert code == 0 and not marker.exists(), "late untrusted project MCP must never start"
    config = ctx.config_path.read_bytes()
    ctx.close()
    # Only this synthetic fixture is made trusted, after all config leases release.
    # The same controlled server must start, proving the negative control was active.
    ctx.config_path.write_bytes(config.replace(b'trust_level = "untrusted"', b'trust_level = "trusted"'))
    code, _, _ = await run_capture(argv, env=environment, cwd=str(task), timeout=30)
    assert code == 0 and marker.read_text() == "STARTED", "trusted fixture positive control failed"


async def test_native_planted_hardlink_cannot_expose_or_modify_outside_file(native_context):
    ctx, _adapter, task, _ = native_context
    outside = task.parent / "outside-hardlink-canary.txt"
    outside.write_text("SYNTHETIC-OUTSIDE")
    alias = task / "planted-hardlink.txt"
    os.link(outside, alias)
    assert alias.read_text() == "SYNTHETIC-OUTSIDE" and outside.stat().st_nlink == 2
    original_security = acl.read_security(outside)
    with pytest.raises(rt.CodexRuntimeUnavailable):
        ctx.prepare(fake_auth())
    assert ctx._home is None  # Refusal happens before home/auth creation or any CLI launch.
    assert outside.read_text() == "SYNTHETIC-OUTSIDE"
    assert acl.read_security(outside) == original_security


@pytest.mark.parametrize("metadata_directory", [False, True])
def test_native_metadata_hardlinks_are_refused_before_auth_creation(native_context, metadata_directory):
    ctx, _, task, _ = native_context
    outside = task.parent / "metadata-canary.txt"
    outside.write_text("SYNTHETIC-METADATA-CANARY")
    metadata = task / ".git"
    metadata.rename(task / "original-gitlink.saved")  # Preserve this disposable fixture's original link.
    if metadata_directory:
        metadata.mkdir()
        metadata /= "alias.txt"
    os.link(outside, metadata)
    assert metadata.read_text() == "SYNTHETIC-METADATA-CANARY"
    with pytest.raises(rt.CodexRuntimeUnavailable):
        ctx.prepare(fake_auth())
    assert ctx._home is None and ctx not in rt._HELD
    assert outside.read_text() == "SYNTHETIC-METADATA-CANARY"


async def test_native_worker_cannot_create_readable_cross_scope_hardlink(native_context):
    ctx, adapter, task, _ = native_context
    outside_dir = task.parent / "outside"
    outside_dir.mkdir()
    outside = outside_dir / "active-link-canary.txt"
    outside.write_text("SYNTHETIC-ACTIVE-LINK")
    original_security = acl.read_security(outside)
    ctx.prepare(fake_auth())
    command = str(Path(os.environ["SYSTEMROOT"]) / "System32" / "cmd.exe")
    # The prepared context holds a directory guard on the task root (no write sharing), which blocks every
    # link/rename INTO the root for any process. Use an unguarded subdirectory so the worker's attempt is
    # decided by the Codex sandbox, not by that guard.
    linkdir = task / "linkdir"
    linkdir.mkdir()
    host_alias = linkdir / "host-hardlink-positive.txt"
    assert _cmd_safe(host_alias) and _cmd_safe(outside)
    # Host positive control at the exact location the worker will use: proves the link is creatable there
    # (no sharing violation, valid syntax), so a worker failure below is the sandbox's denial.
    code, hout, herr = await run_capture([command, "/d", "/c", f"mklink /H {host_alias} {outside}"],
                                   env=ctx.environment, cwd=str(task), timeout=10)
    assert code == 0 and host_alias.read_text() == "SYNTHETIC-ACTIVE-LINK", (hout, herr)
    host_alias.unlink()
    prefix = adapter.resolve().argv_prefix

    async def attack(text):
        return await run_capture([*prefix, "sandbox", "--include-managed-config", "-P", "orch-write",
            "-C", str(task), "--", command, "/d", "/c", text], env=ctx.environment, cwd=str(task), timeout=30)

    code, _, _ = await attack('echo OK>linkdir\\worker-positive.txt')
    assert code == 0 and (linkdir / "worker-positive.txt").exists(), "worker write positive control failed"
    code, _, _ = await attack(f'mklink /H linkdir\\created-cross-scope.txt {outside}')
    if code == 0:
        code, _, _ = await attack('type linkdir\\created-cross-scope.txt')
        assert code != 0, "worker-created hardlink exposed an outside object"
        code, _, _ = await attack('echo BAD>linkdir\\created-cross-scope.txt')
        assert code != 0, "worker-created hardlink modified an outside object"
    assert outside.read_text() == "SYNTHETIC-ACTIVE-LINK"
    assert acl.read_security(outside) == original_security
