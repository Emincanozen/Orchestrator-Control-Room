"""Process layer: env allowlist, shim resolution, streaming, timeout and real process-tree cancellation."""

from __future__ import annotations

import asyncio
import sys
import textwrap
import time
from pathlib import Path

import psutil
import pytest

from orchestrator.providers import process as P
from orchestrator.providers.base import AgentEventKind as K
from orchestrator.providers.base import ErrorCategory as C
from orchestrator.providers.base import RecursionGuardError
from orchestrator.providers.claude import ClaudeAdapter

from .conftest import SECRET_NAME, request

PARENT_SCRIPT = textwrap.dedent(
    """
    import subprocess, sys, time
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    print("CHILD", child.pid, flush=True)
    time.sleep(120)
    """
)


def pid_alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).is_running() and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


async def wait_dead(pids, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not any(pid_alive(p) for p in pids):
            return True
        await asyncio.sleep(0.1)
    return False


# --------------------------------------------------------------------------- env


def test_build_child_env_filters_secrets_and_sets_depth(monkeypatch):
    monkeypatch.setenv(SECRET_NAME, "x")
    env = P.build_child_env(["PATH", "systemroot"], base_env={"Path": "p", "SYSTEMROOT": "r", SECRET_NAME: "x"})
    assert SECRET_NAME not in env and env["AI_ORCHESTRATOR_DEPTH"] == "1"
    assert env["Path"] == "p" and env["SYSTEMROOT"] == "r"  # case-insensitive allowlist (Windows)


def test_depth_increments_and_guard(monkeypatch):
    assert P.build_child_env([], base_env={"AI_ORCHESTRATOR_DEPTH": "0"})["AI_ORCHESTRATOR_DEPTH"] == "1"
    P.check_recursion_guard({})
    with pytest.raises(RecursionGuardError):
        P.check_recursion_guard({"AI_ORCHESTRATOR_DEPTH": "1"})
    with pytest.raises(RecursionGuardError):
        P.check_recursion_guard({"AI_ORCHESTRATOR_DEPTH": "garbage"})


def test_allowlist_loaded_from_policies_yaml():
    names = P.load_env_allowlist()
    assert "PATH" in names and "CODEX_HOME" in names
    assert not any("KEY" in n or "TOKEN" in n or "SECRET" in n for n in names)


async def test_secret_does_not_reach_real_child(monkeypatch, tmp_path):
    monkeypatch.setenv(SECRET_NAME, "leak-me")
    env = P.build_child_env()
    proc = await P.spawn_process(
        [sys.executable, "-c", "import os,json;print(json.dumps(sorted(os.environ)))"], cwd=str(tmp_path), env=env
    )
    lines = [ln async for kind, ln in proc.stream() if kind == "stdout"]
    assert SECRET_NAME not in lines[0] and "AI_ORCHESTRATOR_DEPTH" in lines[0]


# --------------------------------------------------------------------------- shim resolution


def test_resolves_claude_style_shim_to_native_exe(tmp_path):
    exe = tmp_path / "node_modules" / "@vendor" / "claude-code" / "bin" / "claude.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    (tmp_path / "claude.cmd").write_text(
        '@ECHO off\r\nGOTO start\r\n:start\r\nSETLOCAL\r\nCALL :find_dp0\r\n'
        '"%dp0%\\node_modules\\@vendor\\claude-code\\bin\\claude.exe"   %*\r\n', encoding="utf-8")
    P.clear_resolve_cache()
    r = P.resolve_executable("claude", search_path=str(tmp_path))
    assert r.argv_prefix == (str(exe.resolve()),) and "cmd" not in Path(r.argv_prefix[0]).name.lower()


def test_resolves_codex_style_shim_to_node_and_js(tmp_path):
    js = tmp_path / "node_modules" / "@vendor" / "codex" / "bin" / "codex.js"
    js.parent.mkdir(parents=True)
    js.write_text("//", encoding="utf-8")
    (tmp_path / "node.exe").write_bytes(b"MZ")
    (tmp_path / "codex.cmd").write_text(
        '@ECHO off\r\nIF EXIST "%dp0%\\node.exe" (\r\n  SET "_prog=%dp0%\\node.exe"\r\n) ELSE (\r\n  SET "_prog=node"\r\n)\r\n'
        'endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\\node_modules\\@vendor\\codex\\bin\\codex.js" %*\r\n',
        encoding="utf-8")
    P.clear_resolve_cache()
    r = P.resolve_executable("codex", search_path=str(tmp_path))
    assert r.argv_prefix == (str(tmp_path / "node.exe"), str(js.resolve()))


def test_unparsable_shim_fails_instead_of_using_cmd(tmp_path):
    (tmp_path / "weird.cmd").write_text("@echo off\r\nsomething %*\r\n", encoding="utf-8")
    P.clear_resolve_cache()
    with pytest.raises(P.ExecutableResolutionError):
        P.resolve_executable("weird", search_path=str(tmp_path))


def test_missing_executable_fails_clearly(tmp_path):
    P.clear_resolve_cache()
    with pytest.raises(P.ExecutableResolutionError, match="not found"):
        P.resolve_executable("definitely-not-installed-xyz", search_path=str(tmp_path))


def test_shim_pointing_nowhere_fails(tmp_path):
    (tmp_path / "gone.cmd").write_text('"%dp0%\\missing\\gone.exe" %*\r\n', encoding="utf-8")
    P.clear_resolve_cache()
    with pytest.raises(P.ExecutableResolutionError, match="does not exist"):
        P.resolve_executable("gone", search_path=str(tmp_path))


async def test_unresolvable_cli_yields_result_not_exception():
    ad = ClaudeAdapter(resolver=lambda n: (_ for _ in ()).throw(P.ExecutableResolutionError("nope")),
                       environ={}, env_allowlist=[])
    evs = [e async for e in ad.execute(request())]
    assert evs[-1].kind == K.RESULT and evs[-1].data["error_category"] == C.CRASHED.value


# --------------------------------------------------------------------------- streaming + timeout


async def test_streaming_stdin_and_tails(tmp_path):
    code = "import sys; data=sys.stdin.read(); print('echo:'+data.strip()); print('oops', file=sys.stderr); sys.exit(3)"
    proc = await P.spawn_process([sys.executable, "-c", code], cwd=str(tmp_path), env=P.build_child_env(),
                                 stdin_text="héllo wörld\n")
    got = [(k, ln) async for k, ln in proc.stream()]
    assert ("stdout", "echo:héllo wörld") in got and ("stderr", "oops") in got
    assert proc.returncode == 3 and "oops" in proc.stderr_tail and "echo:" in proc.stdout_tail


async def test_tail_is_bounded(tmp_path):
    code = "for i in range(20000): print('x'*50)"
    proc = await P.spawn_process([sys.executable, "-c", code], cwd=str(tmp_path), env=P.build_child_env())
    async for _ in proc.stream():
        pass
    assert len(proc.stdout_tail) <= P.TAIL_MAX_CHARS + 100


async def test_timeout_kills_tree(tmp_path):
    script = tmp_path / "parent.py"
    script.write_text(PARENT_SCRIPT, encoding="utf-8")
    proc = await P.spawn_process([sys.executable, str(script)], cwd=str(tmp_path), env=P.build_child_env(), timeout=2.0)
    child_pid = None
    async for kind, line in proc.stream():
        if line.startswith("CHILD"):
            child_pid = int(line.split()[1])
    assert proc.timed_out and child_pid is not None
    assert await wait_dead([proc.pid, child_pid])


# --------------------------------------------------------------------------- real cancellation


async def test_terminate_tree_kills_child_and_grandchild(tmp_path):
    script = tmp_path / "parent.py"
    script.write_text(PARENT_SCRIPT, encoding="utf-8")
    proc = await P.spawn_process([sys.executable, str(script)], cwd=str(tmp_path), env=P.build_child_env())
    stream = proc.stream()
    kind, line = await asyncio.wait_for(stream.__anext__(), 15)
    child_pid = int(line.split()[1])
    assert pid_alive(proc.pid) and pid_alive(child_pid)
    assert await proc.terminate_tree() is True
    assert await wait_dead([proc.pid, child_pid])
    async for _ in stream:  # stream must terminate once the tree is gone
        pass


async def test_adapter_cancel_real_process_tree(tmp_path):
    script = tmp_path / "parent.py"
    script.write_text(PARENT_SCRIPT, encoding="utf-8")

    async def dummy_spawner(argv, *, cwd, env, stdin_text=None, timeout=None):
        return await P.spawn_process([sys.executable, str(script)], cwd=cwd, env=env, stdin_text=stdin_text,
                                     timeout=timeout)

    ad = ClaudeAdapter(spawner=dummy_spawner, env_allowlist=P.load_env_allowlist(), environ={},
                       resolver=lambda n: P.ResolvedExecutable(("claude",), "claude", "test"))
    events: list = []

    async def consume():
        async for ev in ad.execute(request(run_id="r-cancel", cwd=str(tmp_path))):
            events.append(ev)

    task = asyncio.create_task(consume())
    pid = None
    for _ in range(100):
        pid = ad.pid_for("r-cancel")
        if pid and psutil.pid_exists(pid) and psutil.Process(pid).children():
            break
        await asyncio.sleep(0.1)
    assert pid is not None
    children = [c.pid for c in psutil.Process(pid).children(recursive=True)]
    assert children, "dummy parent must have spawned a child"
    assert await ad.cancel("r-cancel") is True
    await asyncio.wait_for(task, 20)
    assert events[-1].kind == K.RESULT and events[-1].data["error_category"] == C.CANCELLED.value
    assert await wait_dead([pid, *children])
    assert ad.pid_for("r-cancel") is None


async def test_abandoned_generator_does_not_leak_child(tmp_path):
    script = tmp_path / "parent.py"
    script.write_text(PARENT_SCRIPT, encoding="utf-8")
    holder = {}

    async def dummy_spawner(argv, *, cwd, env, stdin_text=None, timeout=None):
        holder["p"] = await P.spawn_process([sys.executable, str(script)], cwd=cwd, env=env)
        return holder["p"]

    ad = ClaudeAdapter(spawner=dummy_spawner, env_allowlist=P.load_env_allowlist(), environ={},
                       resolver=lambda n: P.ResolvedExecutable(("claude",), "claude", "test"))
    gen = ad.execute(request(run_id="r-aband", cwd=str(tmp_path)))
    first = await asyncio.wait_for(gen.__anext__(), 20)  # the dummy prints a line the parser surfaces as RAW
    assert first.kind == K.RAW
    pid = holder["p"].pid
    assert pid_alive(pid)
    await gen.aclose()  # consumer walks away mid-run
    assert await wait_dead([pid])
