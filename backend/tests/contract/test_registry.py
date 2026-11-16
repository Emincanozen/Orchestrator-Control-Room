"""Registry caching and model-list parsing without any real CLI."""

from __future__ import annotations

from orchestrator.providers.antigravity import parse_agy_models
from orchestrator.providers.base import ProviderStatus
from orchestrator.providers.registry import ProviderRegistry

from .conftest import FakeSpawner, make_adapter


async def test_refresh_caches_status_and_models_and_survives_failures():
    ok = make_adapter("claude", FakeSpawner([]))
    bad = make_adapter("codex", FakeSpawner([]))

    async def boom():
        raise RuntimeError("kaput")

    bad.detect = boom  # type: ignore[method-assign]

    async def ok_detect():
        return ProviderStatus(provider="claude", installed=True, version="1")

    ok.detect = ok_detect  # type: ignore[method-assign]
    reg = ProviderRegistry({"claude": ok, "codex": bad})
    st = await reg.refresh()
    assert st["claude"].installed and not st["codex"].installed and "kaput" in st["codex"].error
    assert reg.status("claude").version == "1"
    assert {m.model_id for m in reg.models("claude")} >= {"haiku", "sonnet"}
    assert all(m.source in ("config", "static") for m in reg.models("claude"))
    assert reg.models("codex") == []


def test_default_registry_has_three_providers():
    assert ProviderRegistry.build_default().names() == ["claude", "codex", "antigravity"]


def test_parse_agy_models_output():
    out = ("Fetching available models...\n"
           "gemini-3.8-flash-low\tGemini 3.8 Flash (Low)\n"
           "claude-opus-5-5-high\tClaude Opus 5.5 (High)\n")
    seed = {"gemini-3.8-flash-low": {"tier": "fast"}}
    models = parse_agy_models(out, seed, ["coding"])
    assert [m.model_id for m in models] == ["gemini-3.8-flash-low", "claude-opus-5-5-high"]
    assert models[0].tier == "fast" and models[0].source == "cli" and models[1].display_name == "Claude Opus 5.5 (High)"
