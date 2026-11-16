"""The runtime object owned by the FastAPI app (app.state.rt)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from orchestrator.checkpoints.manager import CheckpointManager
from orchestrator.providers.base import ProviderAdapter
from orchestrator.scheduler.executor import TaskExecutor
from orchestrator.scheduler.scheduler import Scheduler
from orchestrator.security.approvals import ApprovalService
from orchestrator.services.agents import AgentService
from orchestrator.services.context import AppContext
from orchestrator.services.projects import ProjectService
from orchestrator.services.tasks import TaskService


@dataclass
class Runtime:
    ctx: AppContext
    providers: Mapping[str, ProviderAdapter]
    agents: AgentService
    executor: TaskExecutor
    scheduler: Scheduler
    tasks: TaskService
    projects: ProjectService
    approvals: ApprovalService
    checkpoints: CheckpointManager
    token: str
    allowed_origins: set[str]
    allowed_hosts: set[str]
    started_monotonic: float = field(default_factory=time.monotonic)
    recovery_report: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)  # later milestones hang services here
