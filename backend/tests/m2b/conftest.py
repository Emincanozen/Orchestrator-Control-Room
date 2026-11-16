"""Fixtures for M2b: a harness whose executor uses the REAL RoutingService over fake (mock) providers."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Optional

import pytest

from orchestrator.config import get_paths, load_yaml
from orchestrator.providers.base import ProviderCapabilities, ProviderStatus
from orchestrator.providers.mock import MockProvider
from orchestrator.providers.registry import ProviderRegistry
from orchestrator.scheduler.executor import TaskExecutor
from orchestrator.scheduler.scheduler import Scheduler
from orchestrator.security.approvals import ApprovalService
from orchestrator.services.agents import AgentService
from orchestrator.services.context import AppContext
from orchestrator.services.projects import ProjectService
from orchestrator.services.providers import ProviderService, merge_models_cfg, mock_models_cfg
from orchestrator.services.routing import RoutingService
from orchestrator.services.tasks import TaskService
from orchestrator.services.usage import UsageMonitor
from orchestrator.testing import FAST_LIMITS, PASS_CHECK, Harness, py_check

OK = PASS_CHECK


class ResumableMock(MockProvider):
    """A fake adapter that advertises session resume (the plain mock does not)."""

    async def detect(self) -> ProviderStatus:
        st = await super().detect()
        st.capabilities = ProviderCapabilities(**{**st.capabilities.model_dump(), "resume": True})
        return st


@dataclass
class RH(Harness):
    psvc: Optional[ProviderService] = None
    usage: Optional[UsageMonitor] = None
    routing: Optional[RoutingService] = None


async def build(tmp_path, names=("alpha", "beta"), *, max_switches: int = 3, resumable: tuple[str, ...] = (), tiers=None) -> RH:
    paths = get_paths(tmp_path / "data")
    ctx = AppContext.open(paths)
    adapters = {}
    cfg: dict[str, Any] = {}
    for n in names:
        cls = ResumableMock if n in resumable else MockProvider
        adapters[n] = cls(name=n, model=f"{n}-model")
        cfg = merge_models_cfg(cfg, mock_models_cfg(n, f"{n}-model", (tiers or {}).get(n, "frontier")))
    registry = ProviderRegistry(adapters)
    psvc = ProviderService(ctx, registry, cfg)
    usage = UsageMonitor(ctx)
    psvc.usage = usage
    await psvc.refresh()
    routing_cfg = copy.deepcopy(load_yaml("routing.yaml", ctx.config_dir))
    routing_cfg["preferences"] = {"provider_order": list(names), "disabled_providers": [], "disabled_models": []}
    routing_cfg["fallback"]["max_provider_switches_per_task"] = max_switches
    routing = RoutingService(ctx, psvc, usage, routing_cfg=routing_cfg)
    usage.alternative_finder = routing.find_candidate
    agents = AgentService(ctx, celebrate_seconds=0)
    await agents.seed()
    executor = TaskExecutor(ctx, psvc.adapters, router=routing, agents=agents, usage=usage)
    scheduler = Scheduler(ctx, executor, poll_interval=0.05, quota_check=usage.quota_check)
    h = RH(
        ctx=ctx, mock=adapters[names[0]], agents=agents, executor=executor, scheduler=scheduler, tasks=TaskService(ctx),
        projects=ProjectService(ctx), approvals=ApprovalService(ctx), tmp=tmp_path, providers=adapters,
        psvc=psvc, usage=usage, routing=routing,
    )
    await h.set_limits(**FAST_LIMITS)
    return h


@pytest.fixture
async def rh(tmp_path):
    h = await build(tmp_path)
    try:
        yield h
    finally:
        await h.close()


@pytest.fixture
async def make_rh(tmp_path):
    made: list[RH] = []

    async def factory(*args, **kw) -> RH:
        h = await build(tmp_path, *args, **kw)
        made.append(h)
        return h

    yield factory
    for h in made:
        await h.close()
