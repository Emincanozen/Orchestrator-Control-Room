"""RoutingService: the real router behind RouterPort (+ FailureAdvisor).

Gathers everything the pure router needs (task classification, provider statuses, derived usage
states, user preferences, the previous run, the current leader, exclusions, history statistics),
calls `router.route`, and maps the decision to what the executor persists.

Adaptive history (see db/telemetry.history_stats): a *verified run* is an execute/repair run that
ended succeeded|failed whose task LATER reached a passing verification; `successes` are those whose own
status is `succeeded`. Tasks that never passed verification contribute nothing.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from orchestrator.config import load_yaml
from orchestrator.db import records
from orchestrator.db import runs as runs_repo
from orchestrator.db import telemetry
from orchestrator.db.database import Tx
from orchestrator.domain.models import Task
from orchestrator.domain.states import RunPhase, UsageState
from orchestrator.providers.base import ErrorCategory, ExecutionResult
from orchestrator.router import (
    AttemptInfo,
    ExcludedCandidate,
    LeaderIdentity,
    PreviousRun,
    Router,
    RoutingDecision,
    RoutingRequest,
    TaskClassification,
    UserPreferences,
    classify,
    plan_fallback,
)
from orchestrator.scheduler.ports import FailurePlan, RouteChoice, RouteUnavailable
from orchestrator.security import approvals
from orchestrator.services.context import AppContext
from orchestrator.services.providers import ProviderService
from orchestrator.services.usage import UsageMonitor, state_json
from orchestrator.usage import derive_state
from orchestrator.util import loads, to_iso

log = logging.getLogger(__name__)

PREFERENCES_KEY = "routing.preferences"
PREFERENCE_FIELDS = ("provider_order", "disabled_providers", "disabled_models")
DEFAULT_TASK_TYPE = "coding"  # the DB default; indistinguishable from "not chosen", so it is classified


@dataclass
class TaskSpec:
    title: str
    description: str = ""
    task_type: Optional[str] = None
    role: Optional[str] = None
    preferred_provider: Optional[str] = None
    preferred_model: Optional[str] = None

    @classmethod
    def from_task(cls, t: Task) -> "TaskSpec":
        # task_type "coding" is the column default: let the classifier infer a better type from the text.
        return cls(t.title, t.description, None if t.task_type == DEFAULT_TASK_TYPE else t.task_type, t.role,
                   t.preferred_provider, t.preferred_model)


def _decision_detail(cls: TaskClassification, d: RoutingDecision) -> dict[str, Any]:
    dump = d.model_dump(mode="json", exclude={"candidates", "reasons", "rule_ids", "adaptive_applied"})
    return {"classification": cls.model_dump(mode="json"), "decision": dump}


class RoutingService:
    def __init__(
        self,
        ctx: AppContext,
        providers: ProviderService,
        usage: UsageMonitor,
        *,
        routing_cfg: Optional[Mapping[str, Any]] = None,
    ):
        self.ctx = ctx
        self.providers = providers
        self.usage = usage
        self.routing_cfg: dict[str, Any] = dict(routing_cfg) if routing_cfg is not None else load_yaml("routing.yaml", ctx.config_dir)

    # ------------------------------------------------------------------ preferences
    def default_preferences(self) -> dict[str, Any]:
        p = self.routing_cfg.get("preferences") or {}
        return {
            "provider_order": list(p.get("provider_order") or []),
            "disabled_providers": list(p.get("disabled_providers") or []),
            "disabled_models": list(p.get("disabled_models") or []),
        }

    def effective_preferences_tx(self, tx: Tx) -> dict[str, Any]:
        eff = self.default_preferences()
        override = records.settings_all(tx).get(PREFERENCES_KEY)
        if isinstance(override, dict):
            for k in PREFERENCE_FIELDS:
                if isinstance(override.get(k), list):
                    eff[k] = [str(x) for x in override[k]]
        return eff

    # ------------------------------------------------------------------ request building
    async def build_request(
        self,
        spec: TaskSpec,
        phase: str,
        *,
        task: Optional[Task] = None,
        exclude: frozenset[str] = frozenset(),
    ) -> tuple[Router, RoutingRequest]:
        await self.providers.ready()
        policies = await self.ctx.policies()
        statuses = list(self.providers.statuses().values())
        cap = self.providers.capability_registry()
        task_id = task.id if task is not None else None

        def load(tx: Tx) -> dict[str, Any]:
            snaps = self.usage.latest_tx(tx)
            row = tx.one("SELECT provider, model FROM leadership WHERE id = 1")
            leader = None
            if row is not None:
                leader = LeaderIdentity(provider=row["provider"], model=row["model"])
            else:
                configured = (records.settings_all(tx).get("leadership") or {}).get("leader")
                if isinstance(configured, dict) and configured.get("provider"):
                    leader = LeaderIdentity(provider=configured["provider"], model=configured.get("model"))
            runs = runs_repo.for_task(tx, task_id) if task_id else []
            return {
                "snaps": snaps,
                "policy": self.usage.policy_tx(self.ctx, tx),
                "leader": leader,
                "prefs": self.effective_preferences_tx(tx),
                "history": telemetry.history_stats(tx),
                "runs": runs,
                "settings": records.settings_all(tx),
            }

        data = await self.ctx.db.read(load)
        names = sorted({s.provider for s in statuses} | {s.provider for s in data["snaps"]})
        usage_states = [derive_state(data["snaps"], provider=n, policy=data["policy"]) for n in names]
        # model-scoped observations (e.g. a per-model quota) get their own state, so only that model is blocked
        for provider_name, model_name in sorted({(s.provider, s.model) for s in data["snaps"] if s.model and s.state != UsageState.UNKNOWN}):
            usage_states.append(derive_state(data["snaps"], provider=provider_name, model=model_name, policy=data["policy"]))

        prefs = data["prefs"]
        cfg = copy.deepcopy(self.routing_cfg)
        cfg["preferences"] = prefs  # the effective (settings-over-yaml) preferences are authoritative
        # Adaptive routing settings edited from the dashboard (settings key "routing.adaptive") override the yaml.
        from orchestrator.analytics.adaptive import effective_adaptive_cfg

        cfg["adaptive"] = effective_adaptive_cfg(data["settings"], self.routing_cfg)
        router = Router(cfg, policies.data, cap)

        previous: Optional[PreviousRun] = None
        runs = data["runs"]
        if runs:
            last = runs[-1]
            session = next((r.session_id for r in reversed(runs) if r.provider == last.provider and r.session_id), None)
            previous = PreviousRun(provider=last.provider, model=last.model, session_id=session)

        excluded = [ExcludedCandidate(provider=n, model=None, reason="excluded by the caller") for n in sorted(exclude)]
        if task is not None:
            for e in task.route_exclusions:
                try:
                    excluded.append(ExcludedCandidate(**e))
                except Exception:  # noqa: BLE001 - a malformed stored exclusion must not break routing
                    log.warning("ignoring malformed route exclusion %r", e)

        classification = classify(spec.title, spec.description, task_type=spec.task_type, role=spec.role)
        request = RoutingRequest(
            classification=classification,
            phase=phase,  # type: ignore[arg-type]
            statuses=statuses,
            usage=usage_states,
            preferences=UserPreferences(
                provider_order=prefs["provider_order"], disabled_providers=prefs["disabled_providers"],
                disabled_models=prefs["disabled_models"], preferred_provider=spec.preferred_provider,
                preferred_model=spec.preferred_model,
            ),
            previous_run=previous,
            excluded=excluded,
            leader=data["leader"],
            history=data["history"],
        )
        return router, request

    # ------------------------------------------------------------------ decision mapping
    @staticmethod
    def _choice(request: RoutingRequest, d: RoutingDecision) -> RouteChoice:
        assert d.provider is not None
        return RouteChoice(
            provider=d.provider,
            model=d.model,
            reasons=list(d.reasons),
            candidates=[c.model_dump(mode="json") for c in d.candidates],
            rule_ids=list(d.rule_ids),
            adaptive_applied=d.adaptive_applied,
            detail=_decision_detail(request.classification, d),
        )

    @staticmethod
    def outcome_for(d: RoutingDecision, *, approved: bool = False) -> str:
        """route | wait_for_quota | needs_approval | blocked  (no-candidate precedence is explicit)."""
        if d.provider is not None:
            return "needs_approval" if d.requires_approval and not approved else "route"
        if d.wait_for_quota:
            return "wait_for_quota"
        if d.requires_approval and not approved:
            return "needs_approval"
        return "blocked"

    async def _approved(self, task: Optional[Task]) -> bool:
        if task is None:
            return False
        found = await self.ctx.db.read(lambda tx: records.approval_list(tx, "approved", task.id))
        return any(a.kind == approvals.KIND_HIGH_RISK for a in found)

    # ------------------------------------------------------------------ RouterPort
    # ------------------------------------------------------------------ leadership pins (M3)
    async def leadership_pins(self) -> dict[str, Optional[tuple[str, Optional[str]]]]:
        """Configured identities from the single `leadership` row: {'leader': (provider, model)|None,
        'advisor': (provider, model|None)|None}. The row is the source of truth (ARCHITECTURE section 10)."""

        def load(tx: Tx) -> dict[str, Any]:
            row = tx.one("SELECT provider, model, advisor FROM leadership WHERE id = 1")
            if row is None:
                return {"leader": None, "advisor": None}
            adv = loads(row["advisor"], {}) or {}
            return {
                "leader": (row["provider"], row["model"]),
                "advisor": (adv["provider"], adv.get("model")) if adv.get("provider") else None,
            }

        return await self.ctx.db.read(load)

    @staticmethod
    def restrict_to(router: Router, request: RoutingRequest, identity: tuple[str, Optional[str]], why: str) -> None:
        """Exclude every registry candidate except `identity` (model None = every model of that provider)."""
        provider, model = identity
        keep = router.registry.get(provider, model) if model else None
        for e in router.registry.entries():
            if keep is not None:
                same = (e.provider, e.model_id) == (keep.provider, keep.model_id)
            else:
                same = e.provider == provider and (model is None or e.model_id == model)
            if not same:
                request.excluded.append(ExcludedCandidate(provider=e.provider, model=e.model_id, reason=why))

    async def _apply_pin(self, router: Router, request: RoutingRequest, phase: str) -> None:
        """plan/review runs go to the configured leader, advise runs to the configured advisor (if any).
        With nothing configured the router decides as before."""
        if phase not in ("plan", "review", "advise"):
            return
        pins = await self.leadership_pins()
        if phase == "advise":
            if pins["advisor"] is not None:
                self.restrict_to(router, request, pins["advisor"], "not the configured advisor")
            return
        if pins["leader"] is not None:
            request.role = "leader"  # review defaults to the advisor role in the router; the leader reviews
            self.restrict_to(router, request, pins["leader"], "not the configured leader")

    async def route_call(
        self,
        spec: TaskSpec,
        phase: str,
        *,
        role: Optional[str] = None,
        pin: Optional[tuple[str, Optional[str]]] = None,
        task: Optional[Task] = None,
        exclude: frozenset[str] = frozenset(),
    ) -> RouteChoice:
        """Route a leader/advisor call that may belong to no task. `pin` restricts the candidates to one
        identity; the router's tier/capability/usage/billing filters still apply. Raises RouteUnavailable."""
        router, request = await self.build_request(spec, phase, task=task, exclude=exclude)
        if role:
            request.role = role
        if pin is not None:
            self.restrict_to(router, request, pin, "not the requested identity")
        decision = router.route(request)
        if decision.provider is not None:
            return self._choice(request, decision)
        raise RouteUnavailable(
            "wait_for_quota" if decision.wait_for_quota else "blocked",
            decision.no_candidate_reason or "no suitable candidate",
            resets_at=to_iso(decision.resets_at) if decision.resets_at else None,
            detail={**_decision_detail(request.classification, decision), "reasons": list(decision.reasons)},
        )

    async def eligibility(self, role: str, phase: str, spec: Optional[TaskSpec] = None) -> tuple[RoutingDecision, Router]:
        """Router decision for `role` with no pin and no leader preference: `.candidates` are the eligible
        models, `.rejected` says why every other model is not."""
        router, request = await self.build_request(spec or TaskSpec("leadership eligibility", "", "planning"), phase)
        request.role = role
        request.leader = None
        return router.route(request), router

    # ------------------------------------------------------------------ RouterPort
    async def route(self, task: Task, phase: RunPhase, *, exclude: frozenset[str] = frozenset()) -> Optional[RouteChoice]:
        router, request = await self.build_request(TaskSpec.from_task(task), phase.value, task=task, exclude=exclude)
        await self._apply_pin(router, request, phase.value)
        decision = router.route(request)
        gate = phase in (RunPhase.EXECUTE, RunPhase.REPAIR)
        outcome = self.outcome_for(decision, approved=await self._approved(task) if gate else True)
        detail = _decision_detail(request.classification, decision)
        if outcome == "route":
            return self._choice(request, decision)
        reason = decision.no_candidate_reason or "risk critical: explicit approval required before executing"
        if outcome == "needs_approval" and decision.provider is not None:
            reason = "task classified as critical risk: explicit approval required before executing"
            detail["decision"]["would_route_to"] = f"{decision.provider}/{decision.model}"
        raise RouteUnavailable(
            outcome,  # type: ignore[arg-type]
            reason,
            resets_at=to_iso(decision.resets_at) if decision.resets_at else None,
            approval_kind=approvals.KIND_HIGH_RISK if outcome == "needs_approval" else None,
            detail={**detail, "reasons": list(decision.reasons), "rule_ids": list(decision.rule_ids)},
        )

    async def find_candidate(self, task: Task) -> bool:
        """Can the task be placed on some provider right now? (quota_check probe; no side effects)."""
        runs = await self.ctx.db.read(lambda tx: runs_repo.for_task(tx, task.id))
        phase = runs[-1].phase.value if runs else "execute"
        router, request = await self.build_request(TaskSpec.from_task(task), phase, task=task)
        return router.route(request).provider is not None

    async def preview(self, spec: TaskSpec, phase: str = "execute") -> dict[str, Any]:
        router, request = await self.build_request(spec, phase)
        decision = router.route(request)
        return {
            "outcome": self.outcome_for(decision),
            "classification": request.classification.model_dump(mode="json"),
            "decision": decision.model_dump(mode="json"),
        }

    # ------------------------------------------------------------------ FailureAdvisor
    async def plan_failure(self, task: Task, phase: RunPhase, result: ExecutionResult, run_id: str) -> Optional[FailurePlan]:
        def load(tx: Tx) -> list:
            return runs_repo.for_task(tx, task.id)

        runs = await self.ctx.db.read(load)
        failed = next((r for r in runs if r.id == run_id), None) or (runs[-1] if runs else None)
        if failed is None:
            return None
        cat = result.error_category or ErrorCategory.UNKNOWN
        router, request = await self.build_request(TaskSpec.from_task(task), phase.value, task=task)
        model = failed.model or result.model
        entry = router.registry.get(failed.provider, model) if model else None
        model = entry.model_id if entry is not None else model

        # retries already spent on this candidate = consecutive trailing failed runs on it, minus the failing one
        streak = 0
        for r in reversed(runs):
            if r.provider == failed.provider and (r.model or model) == model and r.status.value == "failed":
                streak += 1
            else:
                break
        attempt = AttemptInfo(
            same_candidate_retries=max(0, streak - 1),
            provider_switches=task.provider_switches,
            session_id=result.session_id or failed.session_id,
        )
        previous = RoutingDecision(provider=failed.provider, model=model, phase=phase.value)  # type: ignore[arg-type]
        plan = plan_fallback(previous, cat, attempt, request, router)

        session = None
        action = plan.action
        if action == "resume_same":
            status = self.providers.statuses().get(failed.provider)
            if status is not None and status.capabilities.resume and attempt.session_id:
                session = attempt.session_id
            else:  # adapter cannot resume: a fresh run on the same provider instead
                action = "retry_same"
        target = plan.decision
        detail: dict[str, Any] = {"fallback": {"action": action, "category": plan.category, "reason": plan.reason}}
        if action == "switch" and target is not None:
            detail["decision"] = _decision_detail(request.classification, target)["decision"]
            detail["classification"] = request.classification.model_dump(mode="json")
        return FailurePlan(
            action=action,  # type: ignore[arg-type]
            reason=plan.reason,
            category=plan.category,
            provider=target.provider if target is not None else None,
            model=target.model if target is not None else None,
            session_id=session,
            excluded=[e.model_dump(mode="json") for e in plan.excluded],
            detail=detail,
        )

    # ------------------------------------------------------------------ misc
    async def usage_states(self) -> list[dict[str, Any]]:
        return [{"provider": s.provider, **state_json(s)} for s in await self.usage.states(self.providers.registry.names())]
