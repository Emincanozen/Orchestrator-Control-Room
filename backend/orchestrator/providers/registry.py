"""Provider registry: builds the adapter set, caches status and models, refresh()."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import yaml

from .antigravity import AntigravityAdapter
from .base import ModelInfo, ProviderAdapter, ProviderStatus
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .process import repo_config_dir

PROVIDER_NAMES = ("claude", "codex", "antigravity")


def _task_capabilities(models_yaml: Path | None = None) -> dict[str, list[str]]:
    path = models_yaml or (repo_config_dir() / "models.yaml")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return {k: list(v.get("task_capabilities", [])) for k, v in data["providers"].items()}
    except Exception:  # noqa: BLE001
        return {}


class ProviderRegistry:
    def __init__(self, adapters: dict[str, ProviderAdapter]) -> None:
        self._adapters = dict(adapters)
        self._status: dict[str, ProviderStatus] = {}
        self._models: dict[str, list[ModelInfo]] = {}

    @classmethod
    def build_default(cls, *, models_yaml: Path | None = None, **adapter_kwargs: Any) -> ProviderRegistry:
        caps = _task_capabilities(models_yaml)
        return cls({
            "claude": ClaudeAdapter(task_capabilities=caps.get("claude"), **adapter_kwargs),
            "codex": CodexAdapter(task_capabilities=caps.get("codex"), **adapter_kwargs),
            "antigravity": AntigravityAdapter(task_capabilities=caps.get("antigravity"), **adapter_kwargs),
        })

    def names(self) -> list[str]:
        return list(self._adapters)

    def get(self, name: str) -> ProviderAdapter:
        try:
            return self._adapters[name]
        except KeyError:
            raise KeyError(f"unknown provider {name!r}") from None

    def register(self, adapter: ProviderAdapter) -> None:
        """Add or replace an adapter (used for the mock provider and tests)."""
        self._adapters[adapter.name] = adapter

    def status(self, name: str | None = None) -> Any:
        return self._status.get(name) if name else dict(self._status)

    def models(self, name: str | None = None) -> Any:
        return list(self._models.get(name, [])) if name else {k: list(v) for k, v in self._models.items()}

    async def _refresh_one(self, name: str) -> None:
        adapter = self._adapters[name]
        try:
            status = await adapter.detect()
        except Exception as exc:  # noqa: BLE001 - detection must never take the registry down
            status = ProviderStatus(provider=name, installed=False, error=f"detect failed: {exc}")
        self._status[name] = status
        if not status.installed:
            self._models[name] = []
            return
        try:
            self._models[name] = await adapter.discover_models()
        except Exception:  # noqa: BLE001
            self._models[name] = []

    async def refresh(self, name: str | None = None) -> dict[str, ProviderStatus]:
        """Re-detect (and re-discover models for) one provider or all. Returns the status cache."""
        names = [name] if name else list(self._adapters)
        await asyncio.gather(*(self._refresh_one(n) for n in names))
        return dict(self._status)
