"""Fixtures for the leader/advisor milestone: fake providers driven by a `Brain` (a scripter that answers by
the role marker found in the prompt), the real RoutingService, the real LeaderController. No real CLI is ever
started."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import pytest

from orchestrator.config import get_paths, load_yaml
from orchestrator.db import records
from orchestrator.leader import LeaderStack, build_stack
from orchestrator.providers.base import ExecutionRequest, ProviderCapabilities, ProviderStatus
from orchestrator.providers.mock import MockProvider, MockScript
from orchestrator.providers.registry import ProviderRegistry
from orchestrator.scheduler.executor import TaskExecutor
from orchestrator.scheduler.scheduler import Scheduler
from orchestrator.security.approvals import ApprovalService
from orchestrator.services.agents import AgentService
from orchestrator.services.context import AppContext
from orchestrator.services.projects import ProjectService
from orchestrator.services.providers import ProviderService, merge_models_cfg
from orchestrator.services.routing import RoutingService
from orchestrator.services.tasks import TaskService
from orchestrator.services.usage import UsageMonitor
from orchestrator.testing import FAST_LIMITS, Harness

MARKERS = [
    ("final", "You escalated a review"),
    ("plan", "technical leader planning ONE"),
    ("review", "technical leader reviewing a change"),
    ("advisor", "independent technical advisor"),
    ("decompose", "decomposing a user objective"),
    ("switch", "taking over as the technical leader"),
]

GOOD_PLAN = {
    "deliverables": ["hello.txt"],
    "prerequisites": [],
    "acceptance_criteria": ["hello.txt exists and contains OK"],
    "steps": ["create hello.txt"],
    "verify_commands": [],
    "risks": [],
    "needs_user_input": [],
}
GOOD_REVIEW = {"verdict": "approve", "findings": [], "required_changes": [], "confidence": 0.9}
GOOD_ADVICE = {
    "assessment": "Option 2 is safest given the failing test output.",
    "recommended_option": "Request these changes",
    "risks": ["regression"],
    "disagreements": [],
    "confidence": 0.7,
    "evidence_requested": [],
}
GOOD_DECOMPOSITION = {
    "deliverables": ["hello.txt"],
    "prerequisites": [],
    "acceptance_criteria": ["hello.txt exists"],
    "tasks": [
        {"key": "a", "title": "Create hello.txt", "description": "write OK", "task_type": "coding", "role": "backend", "priority": 60,
         "depends_on": [], "acceptance_criteria": ["file exists"], "verify_commands": []},
        {"key": "b", "title": "Verify hello.txt", "description": "check it", "task_type": "testing", "role": "test", "priority": 50,
         "depends_on": ["a"], "acceptance_criteria": ["check passes"], "verify_commands": ["python -m pytest -q"]},
    ],
}


def kind_of(prompt: str) -> str:
    for kind, marker in MARKERS:
        if marker in prompt:
            return kind
    return "execute"


@dataclass
class Brain:
    """One scripted behaviour shared by the fake providers. Queues are consumed FIFO per kind; when a queue
    is empty the default for that kind answers. Items: dict (structured output), str (raw text, e.g. invalid
    JSON), MockScript, or callable(request) -> MockScript."""

    queues: dict[str, list[Any]] = field(default_factory=dict)
    prompts: dict[str, list[str]] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)  # (provider, kind)
    execute_files: dict[str, str] = field(default_factory=lambda: {"hello.txt": "OK"})
    hooks: dict[str, Callable[[ExecutionRequest], None]] = field(default_factory=dict)

    def push(self, kind: str, *items: Any) -> None:
        self.queues.setdefault(kind, []).extend(items)

    def for_provider(self, name: str) -> Callable[[ExecutionRequest, int], Optional[MockScript]]:
        def scripter(request: ExecutionRequest, _index: int) -> Optional[MockScript]:
            kind = kind_of(request.prompt)
            self.prompts.setdefault(kind, []).append(request.prompt)
            self.calls.append((name, kind))
            if kind in self.hooks:
                self.hooks[kind](request)
            q = self.queues.get(kind) or []
            item = q.pop(0) if q else self.default(kind, request)
            if isinstance(item, MockScript):
                return item
            if callable(item):
                return item(request)
            if isinstance(item, str):
                return MockScript.success(item)
            return MockScript.success(json.dumps(item), structured_output=item)

        return scripter

    def default(self, kind: str, request: ExecutionRequest) -> Any:
        if kind == "plan":
            return GOOD_PLAN
        if kind == "review":
            return GOOD_REVIEW
        if kind == "advisor":
            return GOOD_ADVICE
        if kind == "decompose":
            return GOOD_DECOMPOSITION
        if kind == "final":
            return {"verdict": "approve", "rationale": "verification passed", "evidence_refs": [], "required_changes": []}
        if kind == "switch":  # restate exactly the ids of the checkpoint in the prompt
            m = re.search(r'^(\{"tasks":.*)$', request.prompt, re.M)
            data = json.loads(m.group(1)) if m else {"tasks": []}
            return {"tasks": [{"id": t["id"], "next_action": "continue"} for t in data["tasks"]]}
        return MockScript.success("done", files=dict(self.execute_files))


class CapMock(MockProvider):
    """Mock whose detect() advertises given task capabilities (kept for readability in tests)."""


def models_cfg(spec: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """spec: provider -> {models: [(id, tier)], caps: [...], billing: str}"""
    providers: dict[str, Any] = {}
    for name, s in spec.items():
        providers[name] = {
            "billing_class": s.get("billing", "local"),
            "task_capabilities": s.get("caps", ["coding", "planning", "review", "research", "documentation"]),
            "models": [{"id": mid, "tier": tier, "verified": s.get("verified", True)} for mid, tier in s["models"]],
        }
    return {"providers": providers}


@dataclass
class LH(Harness):
    psvc: Optional[ProviderService] = None
    usage: Optional[UsageMonitor] = None
    routing: Optional[RoutingService] = None
    stack: Optional[LeaderStack] = None
    brain: Optional[Brain] = None

    async def set_leader(self, provider: str = "alpha", model: Optional[str] = None) -> None:
        model = model or f"{provider}-model"
        await self.ctx.db.write(lambda tx: self.stack.leadership.set_leader_tx(tx, provider, model, source="test", reason="test", expect=None))

    async def require_review(self) -> None:
        await self.ctx.db.write(lambda tx: records.setting_put(tx, "review", {"required": True}))

    async def put_setting(self, key: str, value: Any) -> None:
        cur = await self.ctx.db.read(records.settings_all)
        merged = {**(cur.get(key) or {}), **value} if isinstance(value, dict) else value
        await self.ctx.db.write(lambda tx: records.setting_put(tx, key, merged))

    async def mk_task(self, project_id: str, **kw: Any):
        from orchestrator.domain.models import TaskCreate

        kw.setdefault("title", "demo task")
        return await self.tasks.create(TaskCreate(project_id=project_id, **kw))

    def events(self, etype: str) -> list[dict[str, Any]]:
        rows = self.ctx.db.read_sync(lambda tx: tx.all("SELECT * FROM events WHERE type = ? ORDER BY seq", (etype,)))
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out


async def build(
    tmp_path,
    *,
    spec: Optional[dict[str, dict[str, Any]]] = None,
    brain: Optional[Brain] = None,
    leader: Optional[tuple[str, str]] = ("alpha", "alpha-model"),
    start_scheduler: bool = False,
) -> LH:
    spec = spec or {"alpha": {"models": [("alpha-model", "frontier")]}, "beta": {"models": [("beta-model", "frontier")]}}
    brain = brain or Brain()
    paths = get_paths(tmp_path / "data")
    ctx = AppContext.open(paths)
    adapters: dict[str, MockProvider] = {}
    cfg: dict[str, Any] = {}
    for name in spec:
        adapters[name] = MockProvider(name=name, model=spec[name]["models"][0][0], scripter=brain.for_provider(name))
    cfg = merge_models_cfg({}, models_cfg(spec))
    registry = ProviderRegistry(dict(adapters))
    psvc = ProviderService(ctx, registry, cfg)
    usage = UsageMonitor(ctx)
    psvc.usage = usage
    await psvc.refresh()
    routing_cfg = copy.deepcopy(load_yaml("routing.yaml", ctx.config_dir))
    routing_cfg["preferences"] = {"provider_order": list(spec), "disabled_providers": [], "disabled_models": []}
    routing = RoutingService(ctx, psvc, usage, routing_cfg=routing_cfg)
    usage.alternative_finder = routing.find_candidate
    agents = AgentService(ctx, celebrate_seconds=0)
    await agents.seed()
    stack = build_stack(ctx, psvc, routing, psvc.adapters, agents, usage)
    executor = TaskExecutor(ctx, psvc.adapters, router=routing, leader=stack.controller, agents=agents, usage=usage)
    scheduler = Scheduler(ctx, executor, poll_interval=0.05, quota_check=usage.quota_check)
    stack.attach_scheduler(scheduler)
    first = adapters[next(iter(adapters))]
    h = LH(
        ctx=ctx, mock=first, agents=agents, executor=executor, scheduler=scheduler, tasks=TaskService(ctx),
        projects=ProjectService(ctx), approvals=ApprovalService(ctx), tmp=tmp_path, providers=adapters,
        psvc=psvc, usage=usage, routing=routing, stack=stack, brain=brain,
    )
    await h.set_limits(**FAST_LIMITS)
    if leader is not None:
        await h.set_leader(*leader)
    if start_scheduler:
        await scheduler.start()
    return h


async def close(h: LH) -> None:
    await h.stack.shutdown()
    await h.close()


@pytest.fixture
async def lh(tmp_path):
    h = await build(tmp_path)
    try:
        yield h
    finally:
        await close(h)


@pytest.fixture
async def make_lh(tmp_path):
    made: list[LH] = []

    async def factory(**kw: Any) -> LH:
        h = await build(tmp_path / f"h{len(made)}", **kw)
        made.append(h)
        return h

    yield factory
    for h in made:
        await close(h)
