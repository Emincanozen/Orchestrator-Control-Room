"""Owned runtime homes must be per invocation, never mutable adapter-global state."""

import asyncio

from orchestrator.providers.process import CommandSpec

from .conftest import BASE_ENV, FakeSpawner, collect, fixture_lines, make_adapter, request


def test_command_environment_captures_input_snapshot():
    environment = {"CODEX_HOME": "owned-first"}
    spec = CommandSpec(args=[], stdin_text=None, environment=environment)
    environment["CODEX_HOME"] = "changed-later"
    assert spec.environment == {"CODEX_HOME": "owned-first"}


async def test_concurrent_commands_keep_distinct_runtime_homes(provider, monkeypatch):
    fixtures = {"claude": "claude_success.jsonl", "codex": "codex_success.jsonl", "antigravity": "agy_success.jsonl"}
    spawner = FakeSpawner(fixture_lines(fixtures[provider]))
    adapter = make_adapter(provider, spawner)
    original = adapter.build_command

    def build(req):
        command = original(req)
        return CommandSpec(args=command.args, stdin_text=command.stdin_text, cleanup=command.cleanup,
                           environment={**adapter.child_env(), "CODEX_HOME": "owned-" + req.run_id})

    monkeypatch.setattr(adapter, "build_command", build)
    results = await asyncio.gather(collect(adapter, request(run_id="one")),
                                   collect(adapter, request(run_id="two")))
    assert all(events[-1].data["success"] for events in results)
    assert sorted(call["env"]["CODEX_HOME"] for call in spawner.calls) == ["owned-one", "owned-two"]
    assert adapter._environ == BASE_ENV


async def test_explicit_empty_environment_does_not_inherit_adapter_environment(monkeypatch):
    spawner = FakeSpawner(fixture_lines("claude_success.jsonl"))
    adapter = make_adapter("claude", spawner)
    monkeypatch.setattr(adapter, "build_command", lambda req: CommandSpec(args=[], stdin_text=None, environment={}))
    await collect(adapter, request())
    assert spawner.calls[0]["env"] == {}
