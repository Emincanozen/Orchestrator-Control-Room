"""Provider service: owns the ProviderRegistry, persists detection results and emits `provider.status`.

    svc = ProviderService(ctx, registry, models_cfg)
    svc.start_background_refresh()        # at startup: never blocks, never fails startup
    await svc.refresh()                   # POST /api/providers/refresh
    svc.adapters                          # live Mapping[name, adapter] handed to the executor
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator, Mapping
from typing import Any, Optional

from orchestrator.db import telemetry
from orchestrator.db.database import Tx
from orchestrator.domain import events as ev
from orchestrator.providers.base import ModelInfo, ProviderAdapter, ProviderStatus
from orchestrator.providers.registry import ProviderRegistry
from orchestrator.router import CapabilityRegistry
from orchestrator.services.context import AppContext
from orchestrator.util import now_iso

log = logging.getLogger(__name__)

READY_TIMEOUT_SECONDS = 90.0


class AdapterView(Mapping[str, ProviderAdapter]):
    """Read-only live view of the registry's adapters (the executor indexes it by name)."""

    def __init__(self, registry: ProviderRegistry):
        self._registry = registry

    def __getitem__(self, name: str) -> ProviderAdapter:
        try:
            return self._registry.get(name)
        except KeyError:
            raise KeyError(name) from None

    def __iter__(self) -> Iterator[str]:
        return iter(self._registry.names())

    def __len__(self) -> int:
        return len(self._registry.names())


def merge_models_cfg(base: Mapping[str, Any], extra: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """Overlay `extra` providers on the models.yaml mapping (extra never edits an existing provider)."""
    providers = dict((base or {}).get("providers") or {})
    for name, cfg in ((extra or {}).get("providers") or {}).items():
        providers.setdefault(name, cfg)
    return {**(base or {}), "providers": providers}


def mock_models_cfg(name: str = "mock", model: str = "mock-model", tier: str = "frontier") -> dict[str, Any]:
    """Explicit registration of a mock/fake provider for routing (tests, ORCHESTRATOR_ENABLE_MOCK=1).

    It is a *separate* provider section in the models config; the rules for real providers are untouched."""
    return {
        "providers": {
            name: {
                "billing_class": "local",
                "task_capabilities": ["coding", "planning", "review", "research", "documentation"],
                "models": [{"id": model, "tier": tier, "verified": True}],
            }
        }
    }


class ProviderService:
    def __init__(self, ctx: AppContext, registry: ProviderRegistry, models_cfg: Optional[Mapping[str, Any]] = None):
        self.ctx = ctx
        self.registry = registry
        self.models_cfg: dict[str, Any] = dict(models_cfg or {})
        self.adapters = AdapterView(registry)
        self.usage: Any = None  # UsageMonitor, set by the app (records detect() outcomes)
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self.refreshed_at: dict[str, str] = {}

    # ---- lifecycle --------------------------------------------------------
    def start_background_refresh(self) -> None:
        """Detect all providers without blocking the caller. Failures are logged, never raised."""
        if self._task is None:
            self._task = asyncio.create_task(self._safe_refresh(), name="provider-refresh")

    async def _safe_refresh(self) -> None:
        try:
            await self.refresh()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - detection problems must not take the app down
            log.exception("startup provider detection failed")

    async def shutdown(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def ready(self, timeout: float = READY_TIMEOUT_SECONDS) -> None:
        """Wait for the first detection pass (routing needs statuses). Starts one if none is pending."""
        if self.registry.status():
            return
        if self._task is None:
            self.start_background_refresh()
        assert self._task is not None
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            log.warning("provider detection did not finish in %.0fs", timeout)

    # ---- queries ------------------------------------------------------------
    def statuses(self) -> dict[str, ProviderStatus]:
        return self.registry.status()

    def discovered(self) -> list[ModelInfo]:
        return [m for models in self.registry.models().values() for m in models]

    def capability_registry(self) -> CapabilityRegistry:
        return CapabilityRegistry(self.models_cfg, self.discovered())

    def billing_class(self, provider: str) -> str:
        cfg = ((self.models_cfg.get("providers") or {}).get(provider) or {})
        return str(cfg.get("billing_class") or "unknown")

    # ---- refresh ------------------------------------------------------------
    async def refresh(self, name: Optional[str] = None) -> dict[str, ProviderStatus]:
        if name is not None and name not in self.registry.names():
            raise KeyError(name)
        async with self._lock:
            await self.registry.refresh(name)
            names = [name] if name else self.registry.names()
            cap = self.capability_registry()
            for n in names:
                status = self.registry.status(n)
                if status is not None:
                    await self._persist(n, status, cap)
        return self.statuses()

    async def _persist(self, name: str, status: ProviderStatus, cap: CapabilityRegistry) -> None:
        ts = now_iso()
        models = self.registry.models(name)
        rows = []
        for m in models:
            entry = cap.get(name, m.model_id)
            rows.append(
                {
                    "model_id": entry.model_id if entry is not None else m.model_id,
                    "display_name": m.display_name,
                    "source": m.source,
                    "capabilities": sorted(entry.capabilities) if entry is not None else list(m.capabilities),
                    "tier": entry.tier if entry is not None else m.tier,
                    # verified_at = when the CLI itself listed the model (only source "cli" is observed live)
                    "verified_at": ts if m.source == "cli" else None,
                }
            )
        # de-duplicate alias/id collisions so the (provider, model_id) key stays unique
        unique = {r["model_id"]: r for r in rows}
        payload = {
            "provider": name,
            "installed": status.installed,
            "version": status.version,
            "executable": status.executable,
            "authenticated": status.authenticated,
            "auth_detail": status.auth_detail,
            "error": status.error,
            "capabilities": status.capabilities.model_dump(mode="json"),
            "checked_at": ts,
            "models": sorted(unique),
            "model_count": len(unique),
        }

        def fn(tx: Tx) -> None:
            telemetry.status_upsert(tx, payload)
            telemetry.models_replace(tx, name, unique.values())
            self.ctx.bus.append(tx, ev.PROVIDER_STATUS, payload=payload)

        await self.ctx.db.write(fn)
        self.refreshed_at[name] = ts
        if self.usage is not None:
            try:
                await self.usage.on_status(status)
            except Exception:  # noqa: BLE001
                log.exception("recording detect() usage snapshot failed")
