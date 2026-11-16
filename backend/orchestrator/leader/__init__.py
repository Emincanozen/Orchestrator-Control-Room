"""Leader and advisor intelligence (M3). See ARCHITECTURE section 10 and ADR-008.

    stack = build_stack(ctx, psvc, routing, providers, agents, usage)
    executor = TaskExecutor(ctx, providers, router=routing, leader=stack.controller, ...)
    stack.attach_scheduler(scheduler)          # needed by the switch protocol (assignment_paused)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from orchestrator.leader.advisor import AdvisorService
from orchestrator.leader.controller import LeaderController
from orchestrator.leader.decisions import DecisionService
from orchestrator.leader.objectives import ObjectiveService
from orchestrator.leader.runner import CallRunner
from orchestrator.leader.service import LeadershipService
from orchestrator.leader.switch import SwitchService
from orchestrator.providers.base import ProviderAdapter
from orchestrator.services.agents import AgentService
from orchestrator.services.context import AppContext
from orchestrator.services.providers import ProviderService
from orchestrator.services.routing import RoutingService


@dataclass
class LeaderStack:
    ctx: AppContext
    leadership: LeadershipService
    runner: CallRunner
    advisor: AdvisorService
    controller: LeaderController
    decisions: DecisionService
    objectives: ObjectiveService
    switch: Optional[SwitchService] = None
    usage: Any = None

    def attach_scheduler(self, scheduler: Any) -> SwitchService:
        self.switch = SwitchService(self.ctx, self.leadership, self.runner, scheduler, self.usage)
        self.controller.switcher = self.switch
        return self.switch

    async def shutdown(self) -> None:
        await self.objectives.shutdown()
        await self.runner.cancel_all()


def build_stack(
    ctx: AppContext,
    psvc: ProviderService,
    routing: RoutingService,
    providers: Mapping[str, ProviderAdapter],
    agents: AgentService,
    usage: Any = None,
) -> LeaderStack:
    leadership = LeadershipService(ctx, psvc, routing, usage)
    runner = CallRunner(ctx, providers, routing, agents, usage)
    advisor = AdvisorService(ctx, leadership, runner)
    controller = LeaderController(ctx, leadership, advisor)
    return LeaderStack(
        ctx=ctx, leadership=leadership, runner=runner, advisor=advisor, controller=controller,
        decisions=DecisionService(ctx), objectives=ObjectiveService(ctx, leadership, runner), usage=usage,
    )


__all__ = ["LeaderStack", "build_stack"]
