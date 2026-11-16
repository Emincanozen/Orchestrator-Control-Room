"""M9a: Codex `isolate_user_tools` (no user plugins / MCP servers per run). Argv contract + discovery, no real CLI."""

from __future__ import annotations

import json
import tomllib

import pytest

import orchestrator.providers.codex as codex_mod
from orchestrator.providers.base import ExecutionRequest
from orchestrator.providers.codex import CodexAdapter

from .conftest import FakeSpawner, make_adapter, request


def _req(**kw):
    return ExecutionRequest(run_id="r", prompt="p", cwd=r"C:\work", permission="workspace_write", **kw)


def _pairs(args, flag):
    return [args[i + 1] for i, a in enumerate(args) if a == flag]


def _disabled(args):
    values = [value for value in _pairs(args, "-c") if value.startswith("mcp_servers=")]
    return tomllib.loads(values[0])["mcp_servers"] if values else {}


def test_isolation_is_on_by_default_and_disables_plugins_without_touching_other_flags():
    args = CodexAdapter().build_command(_req(model="gpt-6.1-sol")).args
    assert _pairs(args, "--disable") == ["hooks", "plugins", "apps"]
    assert args[:2] == ["exec", "--skip-git-repo-check"] and "--json" in args
    assert args[args.index("-s") + 1] == "workspace-write" and args[args.index("-m") + 1] == "gpt-6.1-sol"
    assert "--ignore-user-config" not in args  # would also drop auth-adjacent/sandbox settings: deliberately not used
    assert not any("mcp_servers" in a for a in args)  # nothing discovered yet


def test_isolation_can_be_turned_off_and_leaves_the_old_argv_unchanged():
    on = CodexAdapter().build_command(_req()).args
    off = CodexAdapter(isolate_user_tools=False).build_command(_req()).args
    # the security flags (--ignore-rules, --disable hooks; finding #4) stay on either way
    assert _pairs(off, "--disable") == ["hooks"] and "--ignore-rules" in off
    for feature in ("plugins", "apps"):
        i = on.index(feature)
        on = on[:i - 1] + on[i + 1:]
    assert on == off


def test_discovered_user_mcp_servers_are_each_disabled_by_override():
    ad = CodexAdapter()
    ad.user_mcp_servers = ["node_repl", "my-server"]
    args = ad.build_command(_req()).args
    assert _disabled(args) == {"node_repl": {"enabled": False}, "my-server": {"enabled": False}}


def test_resume_path_also_carries_the_isolation_flags_and_keeps_sandbox_override():
    ad = CodexAdapter()
    ad.user_mcp_servers = ["node_repl"]
    args = ad.build_command(_req(resume_session_id="thread-1")).args
    assert args[:2] == ["exec", "resume"] and _pairs(args, "--disable") == ["hooks", "plugins", "apps"]
    assert 'sandbox_mode="workspace-write"' in _pairs(args, "-c")
    assert _disabled(args) == {"node_repl": {"enabled": False}}
    assert args[-2:] == ["thread-1", "-"]


async def test_owned_execute_refuses_nonempty_inventory_instead_of_disabling_names():
    spawner = FakeSpawner([])
    ad = make_adapter("codex", spawner)
    ad.user_mcp_servers = ["node_repl"]
    async def inventory(*args, **kwargs):
        return ("node_repl",)
    ad._mcp_inventory = inventory
    events = [e async for e in ad.execute(request(permission="workspace_write"))]
    assert not spawner.calls
    assert events[-1].data["usage"]["_isolation_error"] is True


@pytest.fixture
def fake_cli(monkeypatch):
    calls = []

    async def fake_capture(argv, **kw):
        calls.append(argv)
        tail = argv[-3:]
        if "--version" in argv:
            return 0, "codex-cli 0.167.1\n", ""
        if "login" in argv:
            return 0, "Logged in using ChatGPT\n", ""
        if tail == ["mcp", "list", "--json"]:
            return 0, json.dumps([{"name": "node_repl", "enabled": True}, {"name": "off", "enabled": False},
                                  {"name": "we ird", "enabled": True}, {"name": "ok_1", "enabled": True}]), ""
        return 1, "", "unexpected"

    monkeypatch.setattr(codex_mod, "run_capture", fake_capture)
    return calls


async def test_detect_does_not_adopt_mutable_user_server_inventory(fake_cli):
    ad = make_adapter("codex", FakeSpawner([]), stub_mcp=False)
    status = await ad.detect()
    assert status.installed and status.authenticated
    assert ad.user_mcp_servers == []
    assert not [c for c in fake_cli if c[-3:] == ["mcp", "list", "--json"]]


async def test_mcp_list_failure_blocks_execution_instead_of_using_stale_names(fake_cli, monkeypatch):
    spawner = FakeSpawner([])
    ad = make_adapter("codex", spawner, stub_mcp=False)
    await ad.detect()

    async def failing(argv, **kw):
        return (0, "codex-cli 0.167.1", "") if "--version" in argv else ((0, "Logged in", "") if "login" in argv else (1, "", "boom"))

    monkeypatch.setattr(codex_mod, "run_capture", failing)
    await ad.detect()
    assert ad.user_mcp_servers == []
    assert _pairs(ad.build_command(_req()).args, "--disable") == ["hooks", "plugins", "apps"]
    events = [event async for event in ad.execute(request())]
    assert not spawner.calls
    assert events[-1].data["usage"]["_isolation_error"] is True


async def test_no_discovery_when_isolation_is_off(fake_cli):
    ad = make_adapter("codex", FakeSpawner([]))
    ad.isolate_user_tools = False
    await ad.detect()
    assert ad.user_mcp_servers == [] and not [c for c in fake_cli if c[-1] == "--json" and "mcp" in c]
