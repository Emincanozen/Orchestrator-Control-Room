"""Capability-aware, deterministic router (ARCHITECTURE section 9). Plain data in, plain data out.

Algorithm: resolve role/tier/capabilities -> hard filters -> weighted scoring -> deterministic tie-break.
A decision with ``provider is None`` carries ``no_candidate_reason``; the router never returns an
unsuitable pick.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..domain.states import UsageState
from ..providers.base import ProviderStatus
from ..usage import ProviderUsageState
from .adaptive import compute_adjustment
from .capabilities import CapabilityRegistry, ModelEntry, max_tier, tier_rank
from .classifier import TaskClassification

Phase = Literal["plan", "execute", "repair", "review", "advise"]

DEFAULT_WEIGHTS = {
    "tier_fit": 40.0, "user_preference": 25.0, "context_continuity": 15.0,
    "usage_headroom": 15.0, "historical_success": 5.0,
}
BLOCKING_STATES = {UsageState.EXHAUSTED, UsageState.AUTH_ERROR, UsageState.COOLDOWN, UsageState.PROVIDER_ERROR}
QUOTA_STATES = {UsageState.EXHAUSTED, UsageState.COOLDOWN}
_SEVERITY = {
    UsageState.EXHAUSTED: 6, UsageState.AUTH_ERROR: 5, UsageState.COOLDOWN: 4, UsageState.PROVIDER_ERROR: 3,
    UsageState.LOW: 2, UsageState.AVAILABLE: 1, UsageState.UNKNOWN: 0,
}
HEADROOM = {UsageState.AVAILABLE: 1.0, UsageState.UNKNOWN: 0.6, UsageState.LOW: 0.25}
PHASE_ROLE = {"plan": "leader", "advise": "advisor", "review": "advisor"}
PHASE_CAP = {"plan": "planning", "advise": "planning", "review": "review"}


class UserPreferences(BaseModel):
    provider_order: list[str] | None = None  # None -> routing.yaml preferences.provider_order
    disabled_providers: list[str] = Field(default_factory=list)
    disabled_models: list[str] = Field(default_factory=list)  # model ids, or "provider/model"
    preferred_provider: str | None = None  # per-task
    preferred_model: str | None = None  # per-task


class PreviousRun(BaseModel):
    provider: str
    model: str | None = None
    session_id: str | None = None


class LeaderIdentity(BaseModel):
    provider: str
    model: str | None = None


class ExcludedCandidate(BaseModel):
    provider: str
    model: str | None = None  # None = every model of the provider
    reason: str


class RoutingRequest(BaseModel):
    classification: TaskClassification
    phase: Phase = "execute"
    role: str | None = None  # explicit override
    statuses: list[ProviderStatus] = Field(default_factory=list)
    usage: list[ProviderUsageState] = Field(default_factory=list)
    preferences: UserPreferences = Field(default_factory=UserPreferences)
    previous_run: PreviousRun | None = None
    excluded: list[ExcludedCandidate] = Field(default_factory=list)
    leader: LeaderIdentity | None = None
    history: dict[tuple[str, str, str], dict[str, int]] = Field(default_factory=dict)
    allow_listed: bool = False
    allow_lower_tier: bool = False


class RankedCandidate(BaseModel):
    rank: int
    provider: str
    model: str
    tier: str
    score: float
    components: dict[str, float]
    adjustment: float = 0.0
    adjustment_explanation: str = ""
    usage_state: UsageState = UsageState.UNKNOWN


class RejectedCandidate(BaseModel):
    provider: str
    model: str
    filter: str  # first filter that rejected it
    filters: list[str]
    detail: str


class RoutingDecision(BaseModel):
    provider: str | None = None
    model: str | None = None
    tier: str | None = None
    role: str = ""
    phase: Phase = "execute"
    task_type: str = ""
    required_capabilities: list[str] = Field(default_factory=list)
    min_tier: str = "fast"
    target_tier: str = "fast"
    candidates: list[RankedCandidate] = Field(default_factory=list)
    rejected: list[RejectedCandidate] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    adaptive_applied: bool = False
    requires_approval: bool = False
    extra_verification: bool = False
    no_candidate_reason: str | None = None
    wait_for_quota: bool = False
    resets_at: datetime | None = None


class Router:
    def __init__(self, routing_cfg: Mapping[str, Any], policies_cfg: Mapping[str, Any], registry: CapabilityRegistry) -> None:
        self.cfg = routing_cfg or {}
        self.policies = policies_cfg or {}
        self.registry = registry
        w = dict(DEFAULT_WEIGHTS)
        w.update({k: float(v) for k, v in (self.cfg.get("scoring") or {}).items()})
        self.weights = w
        self.allowed_billing = set(
            (self.policies.get("budgets") or {}).get("allow_billing_classes") or ["subscription_cli", "local"]
        )
        ad = self.cfg.get("adaptive") or {}
        self.adaptive_enabled = bool(ad.get("enabled", True))
        self.min_samples = int(ad.get("min_samples", 20))
        self.max_adjustment = float(ad.get("max_adjustment", 10))
        fb = self.cfg.get("fallback") or {}
        self.fallback_cfg = {
            "exclude_failed_candidate": bool(fb.get("exclude_failed_candidate", True)),
            "allow_lower_tier": bool(fb.get("allow_lower_tier", False)),
            "max_provider_switches_per_task": int(fb.get("max_provider_switches_per_task", 3)),
        }

    # ------------------------------------------------------------------ rule resolution
    def _resolve(self, req: RoutingRequest) -> dict[str, Any]:
        cls = req.classification
        roles = self.cfg.get("roles") or {}
        task_types = self.cfg.get("task_types") or {}
        tt_cfg = task_types.get(cls.task_type) or {}
        rule_ids = [f"task_type:{cls.task_type}", f"phase:{req.phase}"]
        reasons: list[str] = []

        if req.role:
            role, why = req.role, "explicit role override"
        elif req.phase in PHASE_ROLE:
            role, why = PHASE_ROLE[req.phase], f"phase '{req.phase}'"
        elif cls.role:
            role, why = cls.role, "role given with the task"
        else:
            role, why = tt_cfg.get("role") or "backend", f"task type '{cls.task_type}'"
        role_cfg = roles.get(role)
        if role_cfg is None:
            role_cfg = {"tier": "standard"}
            reasons.append(f"role '{role}' not in routing.yaml; using tier standard")
        rule_ids.insert(0, f"role:{role}")
        reasons.append(f"role '{role}' selected by {why}")

        # (rule id, tier) components; the role's own component may be relaxed by fallback_tiers
        role_tier = tt_cfg.get("tier") or role_cfg.get("tier", "standard")
        components: list[tuple[str, str]] = [(f"role:{role}", role_tier)]
        if tt_cfg.get("min_tier"):
            components.append((f"task_type:{cls.task_type}.min_tier", tt_cfg["min_tier"]))
        bump = (self.cfg.get("complexity_tier_bump") or {}).get(cls.complexity)
        if bump:
            components.append((f"complexity:{cls.complexity}", bump))
        risk_cfg = (self.cfg.get("risk") or {}).get(cls.risk) or {}
        if risk_cfg.get("min_tier"):
            components.append((f"risk:{cls.risk}", risk_cfg["min_tier"]))

        target = max_tier(*(t for _, t in components))
        fallback_tiers = role_cfg.get("fallback_tiers") or []
        relaxed_role = min([role_tier, *fallback_tiers], key=tier_rank)
        min_tier = max_tier(relaxed_role, *(t for rid, t in components if not rid.startswith("role:")))
        if req.allow_lower_tier:
            min_tier = "fast"
            reasons.append("allow_lower_tier: minimum tier relaxed to fast")
        for rid, t in components:
            reasons.append(f"rule {rid} requires tier >= {t}")
            rule_ids.append(f"{rid}->{t}")
        if tier_rank(min_tier) < tier_rank(target) and not req.allow_lower_tier:
            reasons.append(f"fallback_tiers allow {min_tier} when {target} is unavailable")
        if risk_cfg:
            rule_ids.append(f"risk:{cls.risk}")

        caps = list(cls.required_capabilities)
        if req.phase in PHASE_CAP:
            if req.phase in ("plan", "advise", "review") and "coding" in caps:
                caps.remove("coding")
            if PHASE_CAP[req.phase] not in caps:
                caps.append(PHASE_CAP[req.phase])
        for c in role_cfg.get("capabilities") or []:
            if c not in caps:
                caps.append(c)
        tt_caps = tt_cfg.get("capabilities") or []
        has_visual = any(c in caps for c in (
            "image_generation", "image_editing", "screenshot_analysis", "image_understanding",
            "visual_design", "pixel_art", "animation", "video_generation"))
        for c in tt_caps:
            if cls.task_type == "visual_asset" and has_visual:
                continue  # classification already names the specific visual capability
            if c not in caps:
                caps.append(c)
        return {
            "role": role, "role_cfg": role_cfg, "target": target, "min_tier": min_tier, "caps": caps,
            "rule_ids": rule_ids, "reasons": reasons, "risk_cfg": risk_cfg,
        }

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _usage_for(req: RoutingRequest, entry: ModelEntry) -> ProviderUsageState | None:
        found = [u for u in req.usage if u.provider == entry.provider and u.model in (None, entry.model_id)]
        if not found:
            return None
        return max(found, key=lambda u: (_SEVERITY[u.state], u.model is not None))

    @staticmethod
    def _is_excluded(req: RoutingRequest, entry: ModelEntry) -> ExcludedCandidate | None:
        for ex in req.excluded:
            if ex.provider == entry.provider and ex.model in (None, entry.model_id):
                return ex
        return None

    @staticmethod
    def _is_disabled(prefs: UserPreferences, disabled_p: set[str], disabled_m: set[str], e: ModelEntry) -> bool:
        return e.provider in disabled_p or e.model_id in disabled_m or f"{e.provider}/{e.model_id}" in disabled_m

    # ------------------------------------------------------------------ route
    def route(self, req: RoutingRequest) -> RoutingDecision:
        r = self._resolve(req)
        cls = req.classification
        prefs = req.preferences
        cfg_prefs = self.cfg.get("preferences") or {}
        order = list(prefs.provider_order if prefs.provider_order is not None else cfg_prefs.get("provider_order") or [])
        disabled_p = set(cfg_prefs.get("disabled_providers") or []) | set(prefs.disabled_providers)
        disabled_m = set(cfg_prefs.get("disabled_models") or []) | set(prefs.disabled_models)
        statuses = {s.provider: s for s in req.statuses}
        min_rank = tier_rank(r["min_tier"])

        decision = RoutingDecision(
            role=r["role"], phase=req.phase, task_type=cls.task_type, required_capabilities=r["caps"],
            min_tier=r["min_tier"], target_tier=r["target"], rule_ids=r["rule_ids"], reasons=list(r["reasons"]),
            requires_approval=cls.risk == "critical" and bool(r["risk_cfg"].get("require_approval", cls.risk == "critical")),
            extra_verification=cls.risk in ("high", "critical")
            and bool(r["risk_cfg"].get("extra_verification", True)),
        )
        if decision.requires_approval:
            decision.reasons.append("risk critical: explicit approval required")
        if decision.extra_verification:
            decision.reasons.append(f"risk {cls.risk}: extra verification (independent review) required")

        # Inferred (keyword-detected) capabilities must not override an explicit provider/model preference.
        inferred = [c for c in cls.inferred_capabilities if c in r["caps"] and c not in cls.explicit_capabilities]
        ignored_inferred: list[str] = []

        def preferred_entry(e: ModelEntry) -> bool:
            if prefs.preferred_model and prefs.preferred_model in (e.model_id, f"{e.provider}/{e.model_id}"):
                return True
            return bool(prefs.preferred_provider) and prefs.preferred_provider == e.provider and not prefs.preferred_model

        valid: list[tuple[ModelEntry, ProviderUsageState | None]] = []
        usage_only: list[tuple[ModelEntry, ProviderUsageState]] = []
        for e in self.registry.entries():
            failed: list[str] = []
            details: list[str] = []
            st = statuses.get(e.provider)
            if st is None or not st.installed:
                failed.append("not_installed")
                details.append("provider not installed or no status reported")
            elif st.authenticated is False:
                failed.append("not_authenticated")
                details.append("provider reports not authenticated")
            missing, listed_only = self.registry.missing_capabilities(e, r["caps"], req.allow_listed)
            if missing and inferred and preferred_entry(e):
                relaxed_caps = [c for c in r["caps"] if c not in inferred]
                still, _ = self.registry.missing_capabilities(e, relaxed_caps, req.allow_listed)
                if not still:
                    for c in inferred:
                        if c in missing and c not in ignored_inferred:
                            ignored_inferred.append(c)
                    missing = []
            if missing:
                failed.append("missing_capability")
                msg = f"missing {missing}"
                if listed_only:
                    msg += f" (listed but unverified: {listed_only})"
                details.append(msg)
            us = self._usage_for(req, e)
            if us is not None and us.state in BLOCKING_STATES:
                failed.append(f"usage_{us.state.value.lower()}")
                details.append(f"usage state {us.state.value}: {us.reason}")
            if e.billing_class not in self.allowed_billing:
                failed.append("billing_class")
                details.append(f"billing class '{e.billing_class}' not allowed")
            if self._is_disabled(prefs, disabled_p, disabled_m, e):
                failed.append("disabled")
                details.append("disabled by user/config")
            ex = self._is_excluded(req, e)
            if ex is not None:
                failed.append("excluded")
                details.append(f"excluded: {ex.reason}")
            if tier_rank(e.tier) < min_rank:
                failed.append("below_min_tier")
                details.append(f"tier {e.tier} < required {r['min_tier']}")
            if failed:
                decision.rejected.append(RejectedCandidate(
                    provider=e.provider, model=e.model_id, filter=failed[0], filters=failed, detail="; ".join(details)))
                if us is not None and failed == [f"usage_{us.state.value.lower()}"] and us.state in QUOTA_STATES:
                    usage_only.append((e, us))
            else:
                valid.append((e, us))

        pref_label = (
            f"provider '{prefs.preferred_provider}'" if prefs.preferred_provider and not prefs.preferred_model
            else f"model '{prefs.preferred_model}'" if prefs.preferred_model else "")
        if pref_label:
            if any(preferred_entry(e) for e, _ in valid):
                for c in ignored_inferred:
                    decision.reasons.append(f"inferred capability {c} ignored: explicit preferred {pref_label}")
            else:
                rej = [x for x in decision.rejected
                       if (prefs.preferred_model and prefs.preferred_model in (x.model, f"{x.provider}/{x.model}"))
                       or (not prefs.preferred_model and x.provider == prefs.preferred_provider)]
                fl = sorted({f for x in rej for f in x.filters})
                decision.reasons.insert(
                    0, f"preferred {pref_label} not eligible: {', '.join(fl) if fl else 'no matching model registered'}")

        # soft rule: advisor/reviewer prefers a provider other than the leader's
        if r["role_cfg"].get("prefer_different_provider_than") == "leader" and req.leader is not None:
            different = [v for v in valid if v[0].provider != req.leader.provider]
            if different and len(different) < len(valid):
                for e, _ in valid:
                    if e.provider == req.leader.provider:
                        decision.rejected.append(RejectedCandidate(
                            provider=e.provider, model=e.model_id, filter="same_provider_as_leader",
                            filters=["same_provider_as_leader"], detail="advisor/review prefers a different provider than the leader"))
                valid = different
                decision.reasons.append(f"preferring a provider other than the leader's ({req.leader.provider})")
            elif not different and valid:
                decision.reasons.append("no provider other than the leader's is available; using the leader's provider")

        if not valid:
            decision.no_candidate_reason, decision.wait_for_quota, decision.resets_at = self._no_candidate(
                decision, usage_only, r)
            return decision

        w = self.weights
        n_order = max(len(order), 1)
        ranked: list[tuple[tuple[Any, ...], RankedCandidate]] = []
        any_adaptive = False
        for e, us in valid:
            rank_in_order = order.index(e.provider) if e.provider in order else len(order)
            tier_r = tier_rank(e.tier)
            tgt = tier_rank(r["target"])
            if tier_r == tgt:
                fit = 1.0
            elif tier_r > tgt:
                fit = max(0.0, 1.0 - (tier_r - tgt) / 3.0)
            else:
                fit = 0.5
            pref = 0.5 * (n_order - rank_in_order) / n_order if order else 0.0
            if prefs.preferred_provider and prefs.preferred_provider == e.provider:
                pref += 0.5
            if prefs.preferred_model and prefs.preferred_model in (e.model_id, f"{e.provider}/{e.model_id}"):
                pref += 0.5
            pref = min(1.0, pref)
            ctx = 0.0
            pr = req.previous_run
            if pr and pr.provider == e.provider:
                if pr.model == e.model_id:
                    ctx = 1.0 if pr.session_id else 0.7
                else:
                    ctx = 0.4
            state = us.state if us is not None else UsageState.UNKNOWN
            head = HEADROOM.get(state, 0.6)
            hist = req.history.get((e.provider, e.model_id, cls.task_type))
            cap = min(self.max_adjustment, w["historical_success"]) if w["historical_success"] > 0 else 0.0
            adj = compute_adjustment(
                hist, min_samples=self.min_samples, max_adjustment=cap, enabled=self.adaptive_enabled and cap > 0)
            any_adaptive = any_adaptive or adj.applied
            comps = {
                "tier_fit": round(w["tier_fit"] * fit, 6),
                "user_preference": round(w["user_preference"] * pref, 6),
                "context_continuity": round(w["context_continuity"] * ctx, 6),
                "usage_headroom": round(w["usage_headroom"] * head, 6),
                "historical_success": adj.points,
            }
            score = round(sum(comps.values()), 6)
            cand = RankedCandidate(
                rank=0, provider=e.provider, model=e.model_id, tier=e.tier, score=score, components=comps,
                adjustment=adj.points, adjustment_explanation=adj.explanation if hist is not None else "", usage_state=state)
            ranked.append(((-score, rank_in_order, e.model_id, e.provider), cand))
        ranked.sort(key=lambda x: x[0])
        for i, (_, c) in enumerate(ranked, 1):
            c.rank = i
        decision.candidates = [c for _, c in ranked]
        top = decision.candidates[0]
        decision.provider, decision.model, decision.tier = top.provider, top.model, top.tier
        decision.adaptive_applied = any_adaptive and abs(top.adjustment) > 0
        decision.reasons.append(
            f"chose {top.provider}/{top.model} (tier {top.tier}, score {top.score:g}) among {len(ranked)} eligible; "
            f"target tier {r['target']}, minimum {r['min_tier']}")
        c = top.components
        decision.reasons.append(
            "score parts: " + ", ".join(f"{k}={v:g}" for k, v in c.items()))
        if top.adjustment_explanation:
            decision.reasons.append(f"adaptive: {top.adjustment_explanation}")
        entry = self.registry.get(top.provider, top.model)
        if entry is not None and entry.tier_source == "default":
            decision.reasons.append("model tier is a default (not in models.yaml)")
        return decision

    def _no_candidate(
        self,
        decision: RoutingDecision,
        usage_only: list[tuple[ModelEntry, ProviderUsageState]],
        r: dict[str, Any],
    ) -> tuple[str, bool, datetime | None]:
        if usage_only:
            resets = [u.resets_at for _, u in usage_only if u.resets_at is not None]
            earliest = min(resets) if resets else None
            names = ", ".join(sorted({f"{e.provider}/{e.model_id}" for e, _ in usage_only}))
            reason = f"only usage state blocks routing (exhausted/cooldown): {names}"
            decision.reasons.append(reason + (f"; earliest known reset {earliest.isoformat()}" if earliest else ""))
            return reason, True, earliest
        counts: dict[str, int] = {}
        for rj in decision.rejected:
            counts[rj.filter] = counts.get(rj.filter, 0) + 1
        summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no models known"
        reason = (
            f"no suitable candidate for role '{r['role']}' (tier >= {r['min_tier']}, capabilities {r['caps']}); "
            f"rejections: {summary}"
        )
        decision.reasons.append(reason)
        return reason, False, None
