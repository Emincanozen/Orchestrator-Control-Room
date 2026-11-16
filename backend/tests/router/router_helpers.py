from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml

from orchestrator.domain.states import UsageState
from orchestrator.providers.base import ProviderStatus
from orchestrator.router import RoutingRequest, classify
from orchestrator.usage import ProviderUsageState

CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"


def load(name: str) -> dict:
    return yaml.safe_load((CONFIG_DIR / name).read_text(encoding="utf-8"))


def statuses(*providers: str, **overrides) -> list[ProviderStatus]:
    out = []
    for p in providers or ("claude", "codex", "antigravity"):
        kw = {"installed": True, "authenticated": True}
        kw.update(overrides.get(p, {}))
        out.append(ProviderStatus(provider=p, **kw))
    return out


def usage(provider: str, state: UsageState, model: str | None = None, resets_at: datetime | None = None):
    return ProviderUsageState(provider=provider, model=model, state=state, reason=f"test {state.value}", resets_at=resets_at)


def req(title: str = "implement the repository layer", desc: str = "", **kw) -> RoutingRequest:
    task_type = kw.pop("task_type", None)
    hints = kw.pop("hints", None)
    kw.setdefault("statuses", statuses())
    return RoutingRequest(classification=classify(title, desc, task_type=task_type, hints=hints), **kw)


def at(hour: int) -> datetime:
    return datetime(2026, 11, 12, hour, 0, tzinfo=UTC)


def synthetic_router(routing_cfg, policies_cfg, models: dict[str, list[tuple[str, str]]]):
    """Router over a tiny synthetic registry: {provider: [(model_id, tier), ...]}; all coding/planning/review capable."""
    from orchestrator.router import CapabilityRegistry, Router

    cfg = {"providers": {
        p: {"billing_class": "subscription_cli", "task_capabilities": ["coding", "planning", "review", "research"],
            "models": [{"id": m, "tier": t, "verified": True} for m, t in ms]}
        for p, ms in models.items()}}
    return Router(routing_cfg, policies_cfg, CapabilityRegistry(cfg))
