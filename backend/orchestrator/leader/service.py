"""LeadershipService: the single source of truth for who leads and who advises, and under which limits.

* Identity lives in the one-row `leadership` table (CHECK id = 1, delete-protected): exactly one leader,
  enforced by the database. The advisor identity (optional) is a JSON column of the same row.
* Behaviour knobs live in `settings['leadership']`; execution limits stay in `settings['limits']` /
  policies.yaml (max_leader_calls_per_task, max_consultations_per_task, max_parallel_workers, ...) and are
  only *linked*, never duplicated.
* Eligibility is the router's own: the same hard filters (installed, authenticated, capability, usage,
  billing, disabled, tier) a task would face, so a leader/advisor can never be a model the router would
  refuse. The leader must clear the leader role's minimum tier (routing.yaml: tier with fallback_tiers).
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.db import records
from orchestrator.db.database import Tx
from orchestrator.domain import events as ev
from orchestrator.errors import Conflict, ValidationFailed
from orchestrator.router import tier_rank
from orchestrator.services.context import AppContext
from orchestrator.services.providers import ProviderService
from orchestrator.services.routing import RoutingService
from orchestrator.util import dumps, loads, now_iso

log = logging.getLogger(__name__)

SETTINGS_KEY = "leadership"
ADVISOR_POLICIES = ("never", "on_request", "auto")
FALLBACK_POLICIES = ("off", "same_provider_lower_tier", "any_capable")
ALLOWED_DEFAULT_BILLING = frozenset({"subscription_cli", "local"})  # a default leader is never a metered API

# role -> (router phase used to evaluate it, capability the phase requires)
ROLE_PHASE = {"leader": "plan", "advisor": "review"}


class LeadershipSettings(BaseModel):
    """Everything configurable except identities and the linked policy limits."""

    model_config = ConfigDict(extra="forbid")
    advisor_policy: Literal["never", "on_request", "auto"] = "on_request"
    fallback_policy: Literal["off", "same_provider_lower_tier", "any_capable"] = "off"
    max_consultations_per_day: int = Field(default=10, ge=0, le=1000)
    advisor_after_failed_repairs: int = Field(default=2, ge=1, le=20)  # `auto`: consult after N failed repair cycles
    skip_planning_for_small_tasks: bool = True


class Identity(BaseModel):
    provider: str = Field(min_length=1)
    model: Optional[str] = None


def _ident(provider: Optional[str], model: Optional[str]) -> Optional[dict[str, Any]]:
    return {"provider": provider, "model": model} if provider else None


class LeadershipService:
    def __init__(self, ctx: AppContext, psvc: ProviderService, routing: RoutingService, usage: Any = None):
        self.ctx = ctx
        self.psvc = psvc
        self.routing = routing
        self.usage = usage
        self.switch_in_progress = False
        # Truth about the first-start default for /api/status: "idle" (nothing running), "detecting" (provider
        # detection / default recording in flight) or "none" (finished without an eligible candidate).
        self.default_state: str = "idle"

    async def leader_state(self) -> str:
        """"set" when a leader row exists, else "detecting" while the default is being resolved, else "none"."""
        if await self.leader() is not None:
            return "set"
        return "detecting" if self.default_state == "detecting" else "none"

    # ------------------------------------------------------------------ stored state
    def settings_tx(self, tx: Tx) -> LeadershipSettings:
        raw = records.settings_all(tx).get(SETTINGS_KEY) or {}
        known = {k: v for k, v in raw.items() if k in LeadershipSettings.model_fields}
        try:
            return LeadershipSettings(**known)
        except Exception:  # noqa: BLE001 - a malformed override must not take the leader down
            log.warning("ignoring malformed leadership settings %r", raw)
            return LeadershipSettings()

    async def settings(self) -> LeadershipSettings:
        return await self.ctx.db.read(self.settings_tx)

    def identities_tx(self, tx: Tx) -> dict[str, Any]:
        row = tx.one("SELECT provider, model, advisor, updated_at FROM leadership WHERE id = 1")
        meta = (records.settings_all(tx).get(SETTINGS_KEY) or {}).get("leader_meta") or {}
        if row is None:
            return {"leader": None, "advisor": None, "leader_meta": {}}
        adv = loads(row["advisor"], {}) or {}
        return {
            "leader": {"provider": row["provider"], "model": row["model"]},
            "advisor": _ident(adv.get("provider"), adv.get("model")),
            "leader_meta": {**meta, "updated_at": row["updated_at"]},
        }

    async def identities(self) -> dict[str, Any]:
        return await self.ctx.db.read(self.identities_tx)

    async def leader(self) -> Optional[tuple[str, Optional[str]]]:
        i = (await self.identities())["leader"]
        return (i["provider"], i["model"]) if i else None

    async def limits(self) -> dict[str, int]:
        p = await self.ctx.policies()
        return {
            "max_leader_calls_per_task": p.int_limit("max_leader_calls_per_task"),
            "max_consultations_per_task": p.int_limit("max_consultations_per_task"),
            "max_parallel_workers": p.int_limit("max_parallel_workers"),
            "max_repair_cycles": p.int_limit("max_repair_cycles"),
        }

    # ------------------------------------------------------------------ eligibility
    async def candidates(self, role: str) -> dict[str, Any]:
        """{'min_tier', 'models': [{provider, model, tier, eligible, reasons[]}]} for 'leader' | 'advisor'."""
        await self.psvc.ready()
        decision, router = await self.routing.eligibility(role, ROLE_PHASE[role])
        out: list[dict[str, Any]] = []
        for c in decision.candidates:
            out.append({"provider": c.provider, "model": c.model, "tier": c.tier, "eligible": True, "reasons": [], "score": c.score})
        for r in decision.rejected:
            entry = router.registry.get(r.provider, r.model)
            out.append(
                {"provider": r.provider, "model": r.model, "tier": entry.tier if entry else None, "eligible": False,
                 "reasons": [r.detail or r.filter], "filters": list(r.filters)}
            )
        out.sort(key=lambda m: (not m["eligible"], m["provider"], m["model"]))
        return {"min_tier": decision.min_tier, "required_capabilities": decision.required_capabilities, "models": out}

    async def validate(self, role: Literal["leader", "advisor"], provider: str, model: Optional[str]) -> dict[str, Any]:
        """Return the eligible candidate row or raise ValidationFailed with the reasons it is not eligible."""
        if not model:
            raise ValidationFailed(f"a {role} must name a model", details={"role": role, "provider": provider})
        cands = await self.candidates(role)
        entry = None
        reg = self.psvc.capability_registry().get(provider, model)
        if reg is not None:
            entry = reg
        want = (provider, reg.model_id if reg is not None else model)
        for m in cands["models"]:
            if (m["provider"], m["model"]) == want:
                if m["eligible"]:
                    return m
                raise ValidationFailed(
                    f"{provider}/{model} is not eligible as {role}: {'; '.join(m['reasons'])}",
                    details={"role": role, "provider": provider, "model": model, "reasons": m["reasons"], "min_tier": cands["min_tier"]},
                )
        raise ValidationFailed(
            f"{provider}/{model} is not a known model (not in the capability registry / discovered models)",
            details={"role": role, "provider": provider, "model": model, "min_tier": cands["min_tier"], "entry": bool(entry)},
        )

    # ------------------------------------------------------------------ writes
    def set_leader_tx(
        self, tx: Tx, provider: str, model: Optional[str], *, source: str, reason: str,
        expect: Optional[tuple[str, Optional[str]]] = None, detail: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Compare-and-set the single leader row. `expect` = the identity the caller believes is current
        (None = the row must not exist yet). Returns the previous identity (None for the first)."""
        row = tx.one("SELECT provider, model FROM leadership WHERE id = 1")
        previous = {"provider": row["provider"], "model": row["model"]} if row else None
        if expect is None and row is not None:
            raise Conflict("a leader is already configured", code="leader_exists")
        if expect is not None and (row is None or (row["provider"], row["model"]) != tuple(expect)):
            raise Conflict("the leader changed concurrently", code="leader_changed_concurrently", details={"current": previous})
        ts = now_iso()
        if row is None:
            tx.execute("INSERT INTO leadership (id, provider, model, advisor, updated_at) VALUES (1, ?, ?, '{}', ?)", (provider, model, ts))
        else:
            tx.execute("UPDATE leadership SET provider = ?, model = ?, updated_at = ? WHERE id = 1", (provider, model, ts))
        cur = records.settings_all(tx).get(SETTINGS_KEY) or {}
        records.setting_put(tx, SETTINGS_KEY, {**cur, "leader_meta": {"source": source, "reason": reason, "set_at": ts, **({"detail": detail} if detail else {})}})
        return previous

    async def ensure_default_leader(self) -> Optional[dict[str, Any]]:
        """First start: no leader -> the CHEAPEST eligible candidate: among the router's verified, non-metered
        candidates for the leader role (all already at or above the role's minimum tier) the lowest tier wins;
        ties keep the router's deterministic order. Recorded with reason 'default' and a human `detail`. The user
        can choose any eligible model afterwards. Idempotent; returns the leader (existing or new), or None when
        no eligible verified candidate exists (nothing is invented)."""
        existing = await self.leader()
        if existing is not None:
            return {"provider": existing[0], "model": existing[1]}
        self.default_state = "detecting"
        try:
            return await self._record_default_leader()
        finally:
            self.default_state = "idle" if await self.leader() is not None else "none"

    async def _record_default_leader(self) -> Optional[dict[str, Any]]:
        await self.psvc.ready()
        decision, router = await self.routing.eligibility("leader", ROLE_PHASE["leader"])
        chosen = None
        chosen_tier = None
        pool: list[tuple[int, int, Any, str]] = []
        for i, c in enumerate(decision.candidates):  # already ordered by score, then deterministic tie-breaks
            e = router.registry.get(c.provider, c.model)
            if e is not None and e.verification == "verified" and e.billing_class in ALLOWED_DEFAULT_BILLING:
                pool.append((tier_rank(e.tier), i, c, e.tier))
        if pool:
            _rank, _i, chosen, chosen_tier = min(pool, key=lambda x: (x[0], x[1]))
        if chosen is None:
            await self.ctx.bus.emit(
                ev.SYSTEM_NOTICE,
                payload={"level": "warning", "message": "no verified, non-metered model is eligible as leader; choose one in Settings",
                         "no_candidate_reason": decision.no_candidate_reason},
            )
            return None

        detail = (
            f"cheapest eligible verified model meeting the leader minimum tier ({decision.min_tier}): "
            f"{chosen.provider}/{chosen.model} ({chosen_tier}) of {len(pool)} verified candidate(s); change it in Leadership"
        )

        def fn(tx: Tx) -> Optional[dict[str, Any]]:
            if tx.one("SELECT 1 FROM leadership WHERE id = 1") is not None:
                return None  # lost a race: someone configured a leader meanwhile
            self.set_leader_tx(tx, chosen.provider, chosen.model, source="default", reason="default", expect=None, detail=detail)
            ident = {"provider": chosen.provider, "model": chosen.model}
            self.ctx.bus.append(
                tx, ev.LEADER_CHANGED,
                payload={"from": None, "to": ident, "reason": "default", "detail": detail, "source": "default", "status": "switched",
                         "validated": False, "skip_validation": False, "checkpoint_id": None, "attempts": 0,
                         "candidates_considered": len(decision.candidates)},
            )
            return ident

        made = await self.ctx.db.write(fn)
        if made is None:
            cur = await self.leader()
            return {"provider": cur[0], "model": cur[1]} if cur else None
        return made

    async def update(self, body: "LeadershipUpdate") -> None:
        """PUT /leadership: everything except the leader identity (that is only ever changed by a switch)."""
        fields = body.model_fields_set
        advisor_row: Optional[dict[str, Any]] = None
        set_advisor = "advisor" in fields
        if set_advisor and body.advisor is not None:
            await self.validate("advisor", body.advisor.provider, body.advisor.model)
            reg = self.psvc.capability_registry().get(body.advisor.provider, body.advisor.model or "")
            advisor_row = {"provider": body.advisor.provider, "model": reg.model_id if reg else body.advisor.model}
        limit_fields = {k: getattr(body, k) for k in ("max_leader_calls_per_task", "max_consultations_per_task", "max_parallel_workers") if k in fields and getattr(body, k) is not None}
        setting_fields = {k: getattr(body, k) for k in LeadershipSettings.model_fields if k in fields and getattr(body, k) is not None}

        def fn(tx: Tx) -> None:
            if set_advisor:
                if tx.one("SELECT 1 FROM leadership WHERE id = 1") is None:
                    raise Conflict("configure a leader before an advisor", code="no_leader")
                tx.execute("UPDATE leadership SET advisor = ?, updated_at = ? WHERE id = 1", (dumps(advisor_row or {}), now_iso()))
            current = records.settings_all(tx)
            if setting_fields:
                merged = {**(current.get(SETTINGS_KEY) or {}), **setting_fields}
                LeadershipSettings(**{k: v for k, v in merged.items() if k in LeadershipSettings.model_fields})  # validate
                records.setting_put(tx, SETTINGS_KEY, merged)
            if limit_fields:
                records.setting_put(tx, "limits", {**(current.get("limits") or {}), **limit_fields})

        await self.ctx.db.write(fn)
        if set_advisor:
            await self.ctx.bus.emit(ev.SYSTEM_NOTICE, payload={"level": "info", "message": "advisor configuration changed", "advisor": advisor_row})

    # ------------------------------------------------------------------ views / budgets
    async def budgets(self) -> dict[str, Any]:
        settings = await self.settings()
        limits = await self.limits()
        today = now_iso()[:10]

        def fn(tx: Tx) -> int:
            return int(tx.scalar("SELECT COUNT(*) FROM runs WHERE phase = 'advise' AND substr(started_at, 1, 10) = ?", (today,)) or 0)

        used = await self.ctx.db.read(fn)
        return {
            "consultations_today": {"used": used, "limit": settings.max_consultations_per_day},
            "consultations_per_task": {"limit": limits["max_consultations_per_task"]},
            "leader_calls_per_task": {"limit": limits["max_leader_calls_per_task"]},
            "max_parallel_workers": {"limit": limits["max_parallel_workers"]},
        }

    async def view(self, scheduler: Any = None) -> dict[str, Any]:
        ident = await self.identities()
        settings = await self.settings()
        limits = await self.limits()
        policies = await self.ctx.policies()
        leader = ident["leader"]
        if leader is not None:
            reg = self.psvc.capability_registry().get(leader["provider"], leader["model"] or "")
            leader = {**leader, "tier": reg.tier if reg else None, **{k: v for k, v in ident["leader_meta"].items() if k in ("source", "reason", "set_at", "detail")}}
        cands = {role: await self.candidates(role) for role in ("leader", "advisor")}
        return {
            "leader": leader,
            "advisor": ident["advisor"],
            "advisor_effective": "configured" if ident["advisor"] else ("router" if settings.advisor_policy != "never" else "none"),
            "settings": {
                **settings.model_dump(),
                "max_leader_calls_per_task": limits["max_leader_calls_per_task"],
                "max_consultations_per_task": limits["max_consultations_per_task"],
                "max_parallel_workers": limits["max_parallel_workers"],
            },
            "role_minimum_tier": {role: c["min_tier"] for role, c in cands.items()},
            "budgets": await self.budgets(),
            "permissions": {
                "approvals_required_for": sorted(policies.approvals_required),
                "auto_approve": sorted(policies.auto_approve),
                "worker_permissions": policies.data.get("worker_permissions") or {},
            },
            "candidates": {role: c["models"] for role, c in cands.items()},
            "assignment_paused": bool(getattr(scheduler, "assignment_paused", False)),
            "switch_in_progress": self.switch_in_progress,
        }

    def tier_ok(self, a: str, b: str) -> bool:
        return tier_rank(a) >= tier_rank(b)


class LeadershipUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    advisor: Optional[Identity] = None  # explicit null removes the advisor
    advisor_policy: Optional[Literal["never", "on_request", "auto"]] = None
    fallback_policy: Optional[Literal["off", "same_provider_lower_tier", "any_capable"]] = None
    max_consultations_per_day: Optional[int] = Field(default=None, ge=0, le=1000)
    advisor_after_failed_repairs: Optional[int] = Field(default=None, ge=1, le=20)
    skip_planning_for_small_tasks: Optional[bool] = None
    max_leader_calls_per_task: Optional[int] = Field(default=None, ge=1, le=100)
    max_consultations_per_task: Optional[int] = Field(default=None, ge=0, le=50)
    max_parallel_workers: Optional[int] = Field(default=None, ge=1, le=16)
