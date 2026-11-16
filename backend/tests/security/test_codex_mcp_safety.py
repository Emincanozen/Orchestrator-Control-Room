"""Current CLI metadata, unusual MCP names and fail-closed per-run isolation."""

import asyncio
import hashlib
import json
import os
import tomllib
from pathlib import Path

import pytest

from contract.conftest import FakeSpawner, make_adapter, request
from orchestrator.domain.states import TaskState
from orchestrator.gitops import git
from orchestrator.providers import codex as codex_module
from orchestrator.providers.codex import CodexAdapter
from orchestrator.providers.process import ExecutableResolutionError, resolve_executable
from orchestrator.security import acl, worker_job


def disabled(arguments):
    for index, value in enumerate(arguments[:-1]):
        if value == "-c" and arguments[index + 1].startswith("mcp_servers="):
            return tomllib.loads(arguments[index + 1])["mcp_servers"]
    return {}


def test_unusual_names_are_atomic_toml_keys_not_dotted_paths():
    names = ['server.dot', 'space name', 'quoted"name', 'back\\slash', 'emoji🙂', 'x"} bad=true #']
    adapter = CodexAdapter()
    adapter.user_mcp_servers = names
    arguments = adapter.build_command(request()).args
    assert disabled(arguments) == {name: {"enabled": False} for name in names}


@pytest.mark.parametrize("code,out", [(1, ""), (0, "bad-json"), (0, "{}"), (0, '[{}]'),
                                     (0, '[{"name":null}]'), (0, '[{"name":""}]')])
async def test_unknown_inventory_never_launches_a_model(code, out, monkeypatch):
    calls = []
    async def capture(argv, **kwargs):
        calls.append((argv, kwargs))
        return code, out, ""
    monkeypatch.setattr(codex_module, "run_capture", capture)
    spawner = FakeSpawner([])
    adapter = make_adapter("codex", spawner, stub_mcp=False)
    events = [event async for event in adapter.execute(request())]
    assert not spawner.calls
    assert events[-1].data["usage"]["_isolation_error"] is True
    assert "owned execution unavailable" in events[-1].data["error_message"]
    assert calls[0][1]["cwd"] == request().cwd
    assert calls[0][0][-3:] == ["mcp", "list", "--json"]
    assert "hooks" in calls[0][0] and "plugins" in calls[0][0]


async def test_each_owned_run_refuses_its_nonempty_effective_inventory():
    spawner = FakeSpawner([])
    adapter = make_adapter("codex", spawner)
    adapter.user_mcp_servers = ["stale-global"]
    async def inventory(prefix, env, *, cwd=None):
        await asyncio.sleep(0)
        return ("project.a" if cwd.endswith("a") else "project.b",)
    adapter._mcp_inventory = inventory
    async def consume(run_id, cwd):
        return [event async for event in adapter.execute(request(run_id=run_id, cwd=cwd))]
    results = await asyncio.gather(consume("a", "C:/work-a"), consume("b", "C:/work-b"))
    assert not spawner.calls
    assert all(events[-1].data["usage"]["_isolation_error"] for events in results)
    assert adapter._run_mcp_servers == {} and adapter._owned_contexts == {}


async def test_metadata_containment_failure_is_not_converted_to_empty_inventory(monkeypatch):
    async def capture(*args, **kwargs):
        raise worker_job.ContainmentError("injected unknown metadata job exit")
    monkeypatch.setattr(codex_module, "run_capture", capture)
    with pytest.raises(worker_job.ContainmentError):
        await CodexAdapter()._mcp_inventory(("unused",), {}, cwd="C:/work")


async def test_isolation_failure_blocks_task_without_a_repair_or_provider_spawn(h):
    spawner = FakeSpawner([])
    adapter = make_adapter("codex", spawner)
    async def unavailable(*args, **kwargs):
        return None
    adapter._mcp_inventory = unavailable
    h.executor.providers["mock"] = adapter
    project = await h.project()
    task = await h.task(project.id, verify_commands=[{"file_exists": "result.txt"}], autostart=True)
    assert await h.run_once(task.id) == TaskState.BLOCKED
    current = await h.tasks.get(task.id)
    assert "owned execution unavailable" in current.state_reason
    assert not spawner.calls
    rows = await h.ctx.db.read(lambda tx: tx.all("SELECT status FROM runs WHERE task_id = ?", (task.id,)))
    assert len(rows) == 1 and rows[0]["status"] == "failed"


@pytest.mark.skipif(os.name != "nt", reason="installed Windows CLI metadata, no model calls")
def test_native_cli_disables_user_and_project_mcp_without_starting_them(tmp_path):
    try:
        executable = resolve_executable("codex")
    except ExecutableResolutionError:
        pytest.skip("Codex is optional and not installed; no install attempted")
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    assert acl.harden_private_dir(home)
    git.run_git(["init", "--quiet"], project)
    (project / ".codex").mkdir()
    marker = project / "must-not-start"
    import sys
    import subprocess

    code = f"from pathlib import Path;Path({str(marker)!r}).write_text('fixture-only')"
    server = f"command={json.dumps(sys.executable)}\nargs=[\"-c\",{json.dumps(code)}]\n"
    user_config = home / "config.toml"
    project_config = project / ".codex/config.toml"
    user_config.write_text('[mcp_servers."user.dot"]\n' + server +
        f"[projects.{json.dumps(str(project))}]\ntrust_level=\"trusted\"\n", encoding="utf-8")
    project_config.write_text('[mcp_servers."project.dot"]\n' + server +
                              '[features]\nplugins=true\nhooks=true\napps=true\n', encoding="utf-8")
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (user_config, project_config)}
    env = {k: v for k, v in os.environ.items() if k.upper() in {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "PROGRAMFILES", "PROGRAMFILES(X86)"}}
    env.update({"CODEX_HOME": str(home), "USERPROFILE": str(home), "HOME": str(home),
                "APPDATA": str(home / "appdata"), "LOCALAPPDATA": str(home / "local"),
                "AI_ORCHESTRATOR_DEPTH": "1"})
    adapter = CodexAdapter()
    names = asyncio.run(adapter._mcp_inventory(executable.argv_prefix, env, cwd=str(project)))
    assert names is not None and {"user.dot", "project.dot"}.issubset(names), names
    adapter._run_mcp_servers["metadata-fixture"] = names
    flags = adapter._isolation_args("metadata-fixture")
    # Explicit metadata-only allowlist: never call exec with a prompt here.
    command = [*executable.argv_prefix, "--disable", "hooks", *flags, "mcp", "list", "--json"]
    assert command[-3:] == ["mcp", "list", "--json"]
    result = worker_job.run_confined(command, cwd=str(project), env=env, timeout=30, text=True,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, "CLI rejected the isolation override"
    assert all(row["enabled"] is False for row in json.loads(result.stdout))
    feature_command = [*executable.argv_prefix, "--disable", "hooks", *flags, "features", "list"]
    assert feature_command[-2:] == ["features", "list"]
    features = worker_job.run_confined(feature_command, cwd=str(project), env=env, timeout=30,
        text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    assert features.returncode == 0
    values = {line.split()[0]: line.split()[-1] for line in features.stdout.splitlines() if len(line.split()) >= 3}
    assert values["hooks"] == values["plugins"] == values["apps"] == "false"
    assert not marker.exists(), "a metadata query must never launch configured server code"
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before} == before
