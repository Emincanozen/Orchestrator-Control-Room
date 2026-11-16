from __future__ import annotations

import copy

import pytest
from router_helpers import at, req, statuses, synthetic_router, usage

from orchestrator.domain.states import UsageState as U
from orchestrator.providers.base import ModelInfo
from orchestrator.router import (
    CapabilityRegistry,
    ExcludedCandidate,
    LeaderIdentity,
    PreviousRun,
    Router,
    UserPreferences,
    tier_rank,
)


def rejected(decision, provider, model=None):
    return [r for r in decision.rejected if r.provider == provider and (model is None or r.model == model)]


# ----------------------------------------------------------------------------- registry
def test_registry_listed_capabilities_gated(registry):
    e = registry.get("antigravity", "gemini-4-argon-low")
    assert "image_generation" not in registry.effective_capabilities(e)
    assert "image_generation" in registry.effective_capabilities(e, allow_listed=True)
    assert registry.missing_capabilities(e, ["image_generation"]) == (["image_generation"], ["image_generation"])


def test_registry_unknown_discovered_model_gets_defaults(models_cfg):
    synthetic = {"providers": {"codex": {"billing_class": "subscription_cli", "task_capabilities": ["coding", "planning", "review", "research"],
                                         "models": [{"id": "gpt-6.1-sol", "tier": "advanced", "verified": True}]}}}
    reg = CapabilityRegistry(synthetic, [ModelInfo(provider="codex", model_id="gpt-new", source="cli")])
    e = reg.get("codex", "gpt-new")
    assert (e.tier, e.tier_source, e.verification) == ("standard", "default", "unlisted")
    assert e.capabilities == frozenset(["coding", "planning", "review", "research"])
    assert reg.get("codex", "gpt-6.1-sol").tier_source == "config"


def test_registry_alias_resolution(models_cfg):
    reg = CapabilityRegistry(models_cfg, [ModelInfo(provider="claude", model_id="opus")])
    assert reg.get("claude", "opus").model_id == "claude-opus-5-5"
    assert len([e for e in reg.entries() if e.provider == "claude"]) == 4


def test_registry_unknown_provider_billing_not_allowed(models_cfg, routing_cfg, policies_cfg):
    reg = CapabilityRegistry(models_cfg, [ModelInfo(provider="mystery", model_id="m", capabilities=["coding"])])
    r = Router(routing_cfg, policies_cfg, reg).route(req(statuses=statuses("mystery")))
    assert r.provider is None
    assert rejected(r, "mystery")[0].filter == "billing_class"


# ----------------------------------------------------------------------------- hard filters
def test_filter_not_installed(router):
    r = router.route(req(statuses=statuses("claude", claude={"installed": False})))
    assert all(x.filter == "not_installed" for x in rejected(r, "claude"))
    assert r.provider != "claude"


def test_filter_missing_status_counts_as_not_installed(router):
    r = router.route(req(statuses=statuses("codex")))
    assert rejected(r, "claude")[0].filter == "not_installed"
    assert r.provider == "codex"


def test_filter_unauthenticated_false_rejected_none_allowed(router):
    r = router.route(req(statuses=statuses("claude", "codex", claude={"authenticated": False})))
    assert rejected(r, "claude")[0].filter == "not_authenticated"
    r2 = router.route(req(statuses=statuses("claude", claude={"authenticated": None})))
    assert r2.provider == "claude"


def test_filter_required_capability(router):
    r = router.route(req("analyze this screenshot"))  # screenshot_analysis: only claude has it verified
    assert r.provider == "claude"
    assert rejected(r, "codex")[0].filter == "missing_capability"
    assert rejected(r, "antigravity")[0].filter == "missing_capability"


@pytest.mark.parametrize("state", [U.EXHAUSTED, U.AUTH_ERROR, U.COOLDOWN, U.PROVIDER_ERROR])
def test_filter_usage_states(router, state):
    r = router.route(req(usage=[usage("claude", state)]))
    assert r.provider != "claude"
    assert rejected(r, "claude")[0].filter == f"usage_{state.value.lower()}"


def test_usage_model_specific_state(router):
    r = router.route(req(usage=[usage("claude", U.EXHAUSTED, model="claude-sonnet-5-5")]))
    assert r.model != "claude-sonnet-5-5"
    assert rejected(r, "claude", "claude-sonnet-5-5")


def test_filter_billing_class(routing_cfg, policies_cfg, models_cfg):
    pol = copy.deepcopy(policies_cfg)
    pol["budgets"]["allow_billing_classes"] = ["local"]
    r = Router(routing_cfg, pol, CapabilityRegistry(models_cfg)).route(req())
    assert r.provider is None
    assert {x.filter for x in r.rejected} == {"billing_class"}
    assert r.wait_for_quota is False


def test_filter_disabled_provider_and_model(router):
    r = router.route(req(preferences=UserPreferences(disabled_providers=["claude"])))
    assert r.provider != "claude" and rejected(r, "claude")[0].filter == "disabled"
    r = router.route(req(preferences=UserPreferences(disabled_models=["claude-sonnet-5-5", "antigravity/gemini-4-argon-low"])))
    assert r.model not in ("claude-sonnet-5-5", "gemini-4-argon-low")


def test_filter_config_disabled(routing_cfg, policies_cfg, models_cfg):
    cfg = copy.deepcopy(routing_cfg)
    cfg["preferences"]["disabled_providers"] = ["claude", "antigravity"]
    r = Router(cfg, policies_cfg, CapabilityRegistry(models_cfg)).route(req("design the system architecture"))
    assert r.provider == "codex"


def test_filter_excluded(router):
    r = router.route(req(excluded=[ExcludedCandidate(provider="claude", model="claude-sonnet-5-5", reason="failed")]))
    assert rejected(r, "claude", "claude-sonnet-5-5")[0].filter == "excluded"
    assert r.model != "claude-sonnet-5-5"


def test_filter_below_min_tier(router):
    r = router.route(req("add a retry helper", hints={"complexity": "high"}))
    assert tier_rank(r.tier) >= tier_rank("advanced")
    assert any(x.filter == "below_min_tier" for x in r.rejected)


# ----------------------------------------------------------------------------- tiers
@pytest.mark.parametrize("hints,min_tier", [
    ({"complexity": "low", "risk": "low"}, "standard"),   # role backend is standard
    ({"complexity": "medium", "risk": "low"}, "standard"),
    ({"complexity": "high", "risk": "low"}, "advanced"),
    ({"complexity": "critical", "risk": "low"}, "frontier"),
    ({"complexity": "low", "risk": "high"}, "advanced"),
    ({"complexity": "medium", "risk": "critical"}, "advanced"),
])
def test_tier_minimums(router, hints, min_tier):
    r = router.route(req("add a retry helper", hints=hints))
    assert r.min_tier == min_tier
    if r.provider:
        assert tier_rank(r.tier) >= tier_rank(min_tier)


def test_research_role_fast_tier(router):
    r = router.route(req("research options for a job queue", hints={"complexity": "low"}))
    assert (r.min_tier, r.target_tier, r.tier) == ("fast", "fast", "fast")
    assert r.model == "claude-haiku-5-5"


def test_leader_prefers_frontier_with_advanced_fallback(routing_cfg, policies_cfg):
    rt = synthetic_router(routing_cfg, policies_cfg, {"a": [("a-frontier", "frontier")], "b": [("b-advanced", "advanced")]})
    st = statuses("a", "b")
    r = rt.route(req("plan the milestones for the v2 release", hints={"complexity": "low"}, phase="plan", statuses=st))
    assert (r.target_tier, r.min_tier, r.model) == ("frontier", "advanced", "a-frontier")
    r = rt.route(req("plan the milestones", hints={"complexity": "low"}, phase="plan", statuses=st,
                     excluded=[ExcludedCandidate(provider="a", model="a-frontier", reason="x")]))
    assert (r.tier, r.model) == ("advanced", "b-advanced")


def test_no_frontier_for_critical_complexity_when_absent(routing_cfg, policies_cfg):
    rt = synthetic_router(routing_cfg, policies_cfg, {"a": [("a-adv", "advanced")], "b": [("b-std", "standard")]})
    r = rt.route(req("add a retry helper", hints={"complexity": "critical"}, statuses=statuses("a", "b")))
    assert r.provider is None and "below_min_tier" in r.no_candidate_reason


def test_risk_flags(router):
    low = router.route(req())
    high = router.route(req(hints={"risk": "high"}))
    crit = router.route(req(hints={"risk": "critical"}))
    assert (low.requires_approval, low.extra_verification) == (False, False)
    assert (high.requires_approval, high.extra_verification) == (False, True)
    assert (crit.requires_approval, crit.extra_verification) == (True, True)


# ----------------------------------------------------------------------------- scoring
def test_routine_coding_does_not_pick_frontier(router):
    r = router.route(req("implement the repository layer"))
    assert r.model == "claude-sonnet-5-5" and r.tier == "standard"
    fable = next(c for c in r.candidates if c.model == "claude-fable-5-2")
    assert fable.components["tier_fit"] < r.candidates[0].components["tier_fit"]


def test_over_qualification_penalised_proportionally(router):
    r = router.route(req("implement the repository layer", preferences=UserPreferences(provider_order=["claude"])))
    fit = {c.model: c.components["tier_fit"] for c in r.candidates}
    assert fit["claude-sonnet-5-5"] > fit["claude-opus-5-5"] > fit["claude-fable-5-2"]
    assert fit["claude-sonnet-5-5"] == 40.0


def test_advisor_picks_different_provider_than_leader(router):
    r = router.route(req("review the design", phase="advise", leader=LeaderIdentity(provider="claude", model="claude-fable-5-2")))
    assert r.role == "advisor" and r.provider != "claude"
    assert any(x.filter == "same_provider_as_leader" for x in r.rejected)


def test_advisor_same_provider_when_only_option(router):
    r = router.route(req("review the design", phase="advise", statuses=statuses("claude"),
                         leader=LeaderIdentity(provider="claude")))
    assert r.provider == "claude" and "using the leader's provider" in " ".join(r.reasons)


def test_review_phase_uses_review_capability_and_other_provider(router):
    r = router.route(req("implement the repository layer", phase="review", leader=LeaderIdentity(provider="claude")))
    assert "review" in r.required_capabilities and "coding" not in r.required_capabilities
    assert r.provider != "claude"


def test_user_provider_order(router):
    base = router.route(req("add a retry helper", hints={"complexity": "high"}))
    assert base.provider == "claude"
    r = router.route(req("add a retry helper", hints={"complexity": "high"},
                         preferences=UserPreferences(provider_order=["codex", "claude", "antigravity"])))
    assert r.provider == "codex"


def test_explicit_task_preference_beats_order(router):
    r = router.route(req("implement the repository layer", preferences=UserPreferences(preferred_provider="codex")))
    assert r.tier == "standard" or r.provider == "codex"
    r = router.route(req("add a retry helper", hints={"complexity": "high"},
                         preferences=UserPreferences(preferred_provider="antigravity", preferred_model="gemini-4-argon-high")))
    assert (r.provider, r.model) == ("antigravity", "gemini-4-argon-high")


def test_context_continuity_prefers_same_session(router):
    prefs = UserPreferences(provider_order=["claude", "codex", "antigravity"])
    base = router.route(req("add a retry helper", hints={"complexity": "high"}, preferences=prefs))
    assert base.provider == "claude"
    r = router.route(req("add a retry helper", hints={"complexity": "high"}, preferences=prefs,
                         previous_run=PreviousRun(provider="codex", model="gpt-6.1-sol", session_id="s1")))
    assert (r.provider, r.model) == ("codex", "gpt-6.1-sol")
    assert r.candidates[0].components["context_continuity"] == 15.0


def test_usage_headroom_ordering(router):
    prefs = UserPreferences(provider_order=["claude"])
    r = router.route(req("add a retry helper", hints={"complexity": "high"}, preferences=prefs, usage=[
        usage("claude", U.LOW), usage("codex", U.AVAILABLE), usage("antigravity", U.UNKNOWN)]))
    heads = {c.provider: c.components["usage_headroom"] for c in r.candidates}
    assert heads["codex"] > heads["antigravity"] > heads["claude"]
    assert r.provider in ("claude", "codex")  # headroom alone is smaller than preference gap


def test_headroom_decides_between_equal_candidates(router):
    prefs = UserPreferences(provider_order=[])
    r = router.route(req("add a retry helper", hints={"complexity": "high"}, preferences=prefs,
                         statuses=statuses("codex", "antigravity"), usage=[usage("codex", U.LOW)]))
    assert r.provider == "antigravity"


# ----------------------------------------------------------------------------- quota / no candidate
def test_all_exhausted_sets_wait_for_quota_with_earliest_reset(router):
    r = router.route(req(usage=[
        usage("claude", U.EXHAUSTED, resets_at=at(15)),
        usage("codex", U.COOLDOWN, resets_at=at(12)),
        usage("antigravity", U.EXHAUSTED),
    ]))
    assert r.provider is None and r.wait_for_quota is True
    assert r.resets_at == at(12)
    assert r.no_candidate_reason


def test_auth_error_is_not_wait_for_quota(router):
    r = router.route(req(usage=[usage(p, U.AUTH_ERROR) for p in ("claude", "codex", "antigravity")]))
    assert r.provider is None and r.wait_for_quota is False


def test_wait_for_quota_false_when_other_filter_also_fails(router):
    r = router.route(req("analyze this screenshot", statuses=statuses("claude", "codex"),
                         usage=[usage("claude", U.EXHAUSTED)]))
    assert r.provider is None and r.wait_for_quota is True  # claude is the only capable provider -> quota only
    r2 = router.route(req("analyze this screenshot", statuses=statuses("codex")))
    assert r2.wait_for_quota is False


def test_image_generation_only_listed_unverified_yields_none(router):
    r = router.route(req("generate a logo image"))
    assert r.provider is None and r.wait_for_quota is False
    assert "missing_capability" in r.no_candidate_reason
    ag = rejected(r, "antigravity")[0]
    assert "listed but unverified" in ag.detail


def test_allow_listed_unlocks_image_generation(router):
    r = router.route(req("generate a logo image", allow_listed=True))
    assert r.provider == "antigravity"


def test_visual_asset_config_capability_not_forced_on_screenshot_task(router):
    r = router.route(req("analyze this screenshot"))
    assert "image_generation" not in r.required_capabilities
    assert r.provider == "claude"


def test_animation_in_code_routes_to_coding_provider_without_image_generation(router):
    r = router.route(req("animate the sprite in PixiJS"))
    assert r.provider is not None
    assert "animation" in r.required_capabilities and "image_generation" not in r.required_capabilities


@pytest.mark.parametrize("title", ["create pixel art for the office characters", "generate a promo video"])
def test_asset_production_still_has_no_candidate(router, title):
    r = router.route(req(title))
    assert r.provider is None and r.wait_for_quota is False
    assert "missing_capability" in r.no_candidate_reason


def test_no_candidate_when_nothing_installed(router):
    r = router.route(req(statuses=[]))
    assert r.provider is None and "no suitable candidate" in r.no_candidate_reason


# ----------------------------------------------------------------------------- adaptive in router
def _hist(runs, ok):
    return {("claude", "claude-sonnet-5-5", "coding"): {"verified_runs": runs, "successes": ok}}


def test_adaptive_threshold_in_router(router):
    r19 = router.route(req(history=_hist(19, 19)))
    r20 = router.route(req(history=_hist(20, 20)))
    c19 = next(c for c in r19.candidates if c.model == "claude-sonnet-5-5")
    c20 = next(c for c in r20.candidates if c.model == "claude-sonnet-5-5")
    assert c19.adjustment == 0 and "insufficient evidence" in c19.adjustment_explanation and not r19.adaptive_applied
    assert 0 < c20.adjustment <= 5 and r20.adaptive_applied
    assert "20/20" in " ".join(r20.reasons)


def test_adaptive_can_penalise_failures_but_is_bounded(router):
    r = router.route(req(history=_hist(100, 0)))
    c = next(c for c in r.candidates if c.model == "claude-sonnet-5-5")
    assert -5 <= c.adjustment < 0


# ----------------------------------------------------------------------------- determinism / misc
def test_determinism(router):
    kw = dict(usage=[usage("claude", U.LOW)], preferences=UserPreferences(provider_order=["codex", "claude"]))
    a = router.route(req("add a retry helper", hints={"complexity": "high"}, **kw))
    b = router.route(req("add a retry helper", hints={"complexity": "high"}, **kw))
    assert a.model_dump() == b.model_dump()


def test_tie_break_provider_order_then_model_id(routing_cfg, policies_cfg):
    cfg = {"providers": {
        "a": {"billing_class": "local", "task_capabilities": ["coding"],
              "models": [{"id": "m-b", "tier": "standard"}, {"id": "m-a", "tier": "standard"}]},
        "b": {"billing_class": "local", "task_capabilities": ["coding"], "models": [{"id": "m-0", "tier": "standard"}]},
    }}
    rt = Router(routing_cfg, policies_cfg, CapabilityRegistry(cfg))
    r = rt.route(req(preferences=UserPreferences(provider_order=["a", "b"]), statuses=statuses("a", "b")))
    assert [(c.provider, c.model) for c in r.candidates] == [("a", "m-a"), ("a", "m-b"), ("b", "m-0")]


def test_explicit_role_override(router):
    r = router.route(req("add a retry helper", role="security"))
    assert r.role == "security" and r.min_tier == "advanced"


def test_rule_ids_and_reasons_present(router):
    r = router.route(req())
    assert "role:backend" in r.rule_ids and "task_type:coding" in r.rule_ids
    assert r.reasons and any("chose" in x for x in r.reasons)


# --- explicit preference vs inferred capability ------------------------------------------------------------
def test_preferred_provider_beats_inferred_capability():
    from orchestrator.router import CapabilityRegistry, Router, UserPreferences
    from tests.router.router_helpers import load, req
    router = Router(load("routing.yaml"), load("policies.yaml"), CapabilityRegistry(load("models.yaml")))
    rq = req("Inspect the page", "open the page in the browser and check", preferences=UserPreferences(preferred_provider="claude"))
    assert "browser_inspection" in rq.classification.inferred_capabilities
    d = router.route(rq)
    assert d.provider == "claude", d.reasons
    assert any("inferred capability browser_inspection ignored: explicit preferred provider 'claude'" in x for x in d.reasons)


def test_explicit_capability_is_not_ignored_and_reason_is_prominent():
    from orchestrator.router import CapabilityRegistry, Router, UserPreferences
    from tests.router.router_helpers import load, req
    router = Router(load("routing.yaml"), load("policies.yaml"), CapabilityRegistry(load("models.yaml")))
    rq = req("write docs", "", hints={"required_capabilities": ["browser_inspection"]},
             preferences=UserPreferences(preferred_provider="claude"))
    d = router.route(rq)
    assert d.provider != "claude"
    assert d.reasons[0].startswith("preferred provider 'claude' not eligible: ")
    assert "missing_capability" in d.reasons[0]


def test_preferred_provider_hard_excluded_gives_explicit_reason():
    from orchestrator.router import CapabilityRegistry, Router, UserPreferences
    from tests.router.router_helpers import load, req
    router = Router(load("routing.yaml"), load("policies.yaml"), CapabilityRegistry(load("models.yaml")))
    rq = req("implement the repository layer", preferences=UserPreferences(preferred_provider="codex", disabled_providers=["codex"]))
    d = router.route(rq)
    assert d.provider != "codex"
    assert d.reasons[0].startswith("preferred provider 'codex' not eligible: ")
    assert "disabled" in d.reasons[0]
