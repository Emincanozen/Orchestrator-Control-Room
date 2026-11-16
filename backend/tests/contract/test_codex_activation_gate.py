"""An unactivated containment implementation must never silently use the legacy runner."""

import pytest

from orchestrator.providers import codex as codex_module
from orchestrator.providers.codex import CodexAdapter

from .conftest import BASE_ENV, FakeSpawner, collect, fake_resolver, request


@pytest.mark.parametrize("permission", ["read_only", "workspace_write"])
@pytest.mark.parametrize("isolate", [True, False])
async def test_unactivated_codex_blocks_before_inventory_or_spawn(permission, isolate):
    spawner = FakeSpawner([])
    adapter = CodexAdapter(spawner=spawner, resolver=fake_resolver, environ=dict(BASE_ENV),
                           isolate_user_tools=isolate)

    async def forbidden(*args, **kwargs):
        pytest.fail("unactivated runtime inspected user configuration")

    adapter._mcp_inventory = forbidden
    events = await collect(adapter, request(permission=permission))
    assert not spawner.calls
    result = events[-1].data
    assert result["success"] is False and result["usage"].get("_isolation_error") is True
    assert "blocked" in result["error_message"].lower()


async def test_discovery_reports_execution_block_without_reading_mcp_configuration(monkeypatch):
    calls = []

    async def metadata(argv, **kwargs):
        calls.append(argv)
        if "--version" in argv:
            return 0, "codex-cli 0.167.1", ""
        if "login" in argv:
            return 0, "Logged in using ChatGPT", ""
        pytest.fail("blocked discovery inspected MCP configuration")

    monkeypatch.setattr(codex_module, "run_capture", metadata)
    adapter = CodexAdapter(resolver=fake_resolver, environ=dict(BASE_ENV))
    status = await adapter.detect()
    assert status.installed and status.authenticated
    assert status.error and "blocked" in status.error.lower()
    assert status.capabilities.sandbox_modes == []
    assert len(calls) == 2
