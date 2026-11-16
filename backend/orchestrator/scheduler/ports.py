"""Plug points for later milestones: provider/model routing and the leader.

The executor depends only on these Protocols. Later milestones replace the
defaults (router/router.py, leader/controller.py) without touching the executor:

    executor = TaskExecutor(ctx, providers, router=MyRouter(), leader=MyLeader())

`PhaseRunner` is handed to the leader so that every leader LLM call is a
normal recorded `runs` row (idempotency key, heartbeat, usage, events).
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal, Mapping, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from orchestrator.config import Policies
from orchestrator.domain.models import Task, Verification
from orchestrator.domain.states import RunPhase
from orchestrator.providers.base import AgentEvent, ExecutionResult, ProviderAdapter


class RouteChoice(BaseModel):
    provider: str
    model: Optional[str] = None
    reasons: list[str] = Field(default_factory=list)
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    adaptive_applied: bool = False
    # Full router decision (classification, tiers, rejections, flags); persisted in routing_decisions.detail.
    detail: dict[str, Any] = Field(default_factory=dict)


class RouteUnavailable(Exception):
    """The router has no candidate. `outcome` tells the executor where the task goes:

    wait_for_quota -> WAITING_FOR_QUOTA (not_before from `resets_at`),
    needs_approval -> NEEDS_APPROVAL (an approval of `approval_kind` is requested),
    blocked        -> BLOCKED with `reason`.
    """

    def __init__(
        self,
        outcome: Literal["wait_for_quota", "needs_approval", "blocked"],
        reason: str,
        *,
        resets_at: Optional[str] = None,
        approval_kind: Optional[str] = None,
        detail: Optional[dict[str, Any]] = None,
    ):
        super().__init__(reason)
        self.outcome = outcome
        self.reason = reason
        self.resets_at = resets_at
        self.approval_kind = approval_kind
        self.detail = detail or {}


@runtime_checkable
class RouterPort(Protocol):
    async def route(self, task: Task, phase: RunPhase, *, exclude: frozenset[str] = frozenset()) -> Optional[RouteChoice]:
        """Pick provider+model for this task phase, or None if no suitable candidate exists.

        A router that can say *why* nothing fits raises RouteUnavailable instead of returning None."""
        ...


class FailurePlan(BaseModel):
    """What the router recommends after a failed run (maps router.plan_fallback)."""

    action: Literal["retry_same", "resume_same", "switch", "wait_for_quota", "needs_approval", "block", "fail"]
    reason: str
    category: str = ""
    provider: Optional[str] = None  # target of retry_same / resume_same / switch
    model: Optional[str] = None
    session_id: Optional[str] = None  # only set when the provider's adapter supports resume
    excluded: list[dict[str, Any]] = Field(default_factory=list)  # cumulative exclusions to persist on the task
    detail: dict[str, Any] = Field(default_factory=dict)  # new routing decision (switch) for the audit trail


@runtime_checkable
class FailureAdvisor(Protocol):
    """Optional second port a router may implement; the executor consults it after a failed run."""

    async def plan_failure(
        self, task: Task, phase: RunPhase, result: ExecutionResult, run_id: str
    ) -> Optional[FailurePlan]: ...


@runtime_checkable
class UsageSink(Protocol):
    """Receives every RATE_LIMIT/USAGE event and each run result so usage can be recorded."""

    async def on_event(self, provider: str, model: Optional[str], event: AgentEvent) -> None: ...

    async def on_result(self, provider: str, result: ExecutionResult, *, resets_at: Optional[str] = None) -> None: ...


class PhaseRunner(Protocol):
    async def run_phase(
        self, phase: RunPhase, prompt: str, *, json_schema: Optional[dict[str, Any]] = None
    ) -> ExecutionResult: ...


class ApprovalRequest(BaseModel):
    """A human gate the leader asks the executor to open (task -> NEEDS_APPROVAL + approval row)."""

    kind: str
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)


class PlanOutcome(BaseModel):
    skipped: bool = False
    plan: Optional[Any] = None
    acceptance_criteria: Optional[list[str]] = None
    pending_steps: Optional[list[Any]] = None
    verify_commands: Optional[list[Any]] = None  # only commands that passed the leader's safety filter
    failed: bool = False  # the planning call itself failed (result carries the reason)
    result: Optional[ExecutionResult] = None
    # M3: the plan is stored, then the task parks in NEEDS_APPROVAL (open questions / unsafe proposed commands /
    # exhausted leader-call budget). The executor applies it atomically with the state change.
    needs_approval: Optional[ApprovalRequest] = None


class ReviewEvidence(BaseModel):
    verification: Optional[Verification] = None
    diff_stat: str = ""
    changed_files: list[str] = Field(default_factory=list)
    diff: str = ""  # M3: bounded unified diff against the task's base commit


class ReviewOutcome(BaseModel):
    # M3: "escalate" = the leader could not decide (or the advisor path is closed): the task parks in
    # NEEDS_APPROVAL using `approval`. "reject" = request_changes (feeds the bounded repair loop).
    verdict: Literal["approve", "reject", "escalate", "skipped"]
    notes: str = ""
    issues: list[str] = Field(default_factory=list)
    failed: bool = False
    result: Optional[ExecutionResult] = None
    approval: Optional[ApprovalRequest] = None


@runtime_checkable
class LeaderPort(Protocol):
    def needs_planning(self, task: Task) -> bool: ...

    async def plan(self, task: Task, runner: PhaseRunner) -> PlanOutcome: ...

    def review_required(self, task: Task, policies: Policies) -> bool: ...

    async def review(self, task: Task, evidence: ReviewEvidence, runner: PhaseRunner) -> ReviewOutcome: ...


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #


class DefaultRouter:
    """task.preferred_provider/model if registered, else the first registered provider."""

    def __init__(self, providers: Mapping[str, ProviderAdapter]):
        self.providers = providers

    async def route(self, task: Task, phase: RunPhase, *, exclude: frozenset[str] = frozenset()) -> Optional[RouteChoice]:
        names = [n for n in self.providers if n not in exclude]
        candidates = [{"provider": n, "score": None} for n in names]
        if task.preferred_provider and task.preferred_provider in names:
            return RouteChoice(
                provider=task.preferred_provider,
                model=task.preferred_model,
                reasons=["task preferred provider"],
                candidates=candidates,
                rule_ids=["default.preferred"],
            )
        if not names:
            return None
        reasons = ["first available provider"]
        if task.preferred_provider:
            reasons.insert(0, f"preferred provider {task.preferred_provider!r} unavailable")
        return RouteChoice(provider=names[0], model=None, reasons=reasons, candidates=candidates, rule_ids=["default.first"])


PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {"type": "array", "items": {"type": "string"}},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["steps"],
}
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "reject"]},
        "issues": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
    "required": ["verdict"],
}


def extract_json(result: ExecutionResult) -> Optional[dict[str, Any]]:
    if isinstance(result.structured_output, dict):
        return result.structured_output
    text = result.final_text or ""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


class DefaultLeader:
    """Pass-through planning when criteria exist; review only when policy requires it."""

    def needs_planning(self, task: Task) -> bool:
        return not task.acceptance_criteria and task.plan is None

    async def plan(self, task: Task, runner: PhaseRunner) -> PlanOutcome:
        if not self.needs_planning(task):
            return PlanOutcome(skipped=True)
        prompt = (
            "You are planning a software task. Do not modify files.\n"
            f"Task: {task.title}\n{task.description}\n\n"
            'Reply with JSON only: {"steps": [..], "acceptance_criteria": [..]}'
        )
        result = await runner.run_phase(RunPhase.PLAN, prompt, json_schema=PLAN_SCHEMA)
        if not result.success:
            return PlanOutcome(failed=True, result=result)
        data = extract_json(result) or {}
        steps = [str(s) for s in data.get("steps", []) if isinstance(s, (str, int, float))]
        criteria = [str(s) for s in data.get("acceptance_criteria", []) if isinstance(s, (str, int, float))]
        return PlanOutcome(
            plan={"steps": steps, "notes": None if data else (result.final_text or "")[:4000]},
            acceptance_criteria=criteria or None,
            pending_steps=steps or None,
            result=result,
        )

    def review_required(self, task: Task, policies: Policies) -> bool:
        return policies.review_required

    async def review(self, task: Task, evidence: ReviewEvidence, runner: PhaseRunner) -> ReviewOutcome:
        prompt = (
            "Review the change for this task. Do not modify files. Verification already passed; "
            "judge correctness and requirement coverage.\n"
            f"Task: {task.title}\nRequirements: {task.acceptance_criteria}\n"
            f"Changed files: {evidence.changed_files}\nDiff stat:\n{evidence.diff_stat}\n\n"
            'Reply with JSON only: {"verdict": "approve"|"reject", "issues": [..], "notes": ".."}'
        )
        result = await runner.run_phase(RunPhase.REVIEW, prompt, json_schema=REVIEW_SCHEMA)
        if not result.success:
            return ReviewOutcome(verdict="reject", failed=True, result=result, notes=result.error_message or "")
        data = extract_json(result) or {}
        verdict = data.get("verdict")
        if verdict not in ("approve", "reject"):
            return ReviewOutcome(verdict="reject", notes="review verdict missing or unparseable", result=result)
        issues = [str(i) for i in data.get("issues", []) if isinstance(i, (str, int, float))]
        return ReviewOutcome(verdict=verdict, issues=issues, notes=str(data.get("notes", ""))[:4000], result=result)
