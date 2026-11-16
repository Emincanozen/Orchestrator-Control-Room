from __future__ import annotations

import pytest
from router_helpers import at, req, statuses, synthetic_router, usage

from orchestrator.domain.states import UsageState as U
from orchestrator.providers.base import ErrorCategory as E
from orchestrator.router import (
    AttemptInfo,
    CapabilityRegistry,
    ExcludedCandidate,
    Router,
    compute_adjustment,
    plan_fallback,
)

HIGH = {"complexity": "high"}  # advanced tier: claude opus, codex sol, antigravity advanced models


def run(router, request, cat, attempt=None):
    prev = router.route(request)
    assert prev.provider is not None
    return prev, plan_fallback(prev, cat, attempt or AttemptInfo(), request, router)


@pytest.mark.parametrize("cat", [E.RATE_LIMIT, E.QUOTA_EXHAUSTED])
def test_quota_switches_to_other_provider(router, cat):
    prev, plan = run(router, req("add a retry helper", hints=HIGH), cat)
    assert plan.action == "switch" and plan.decision.provider != prev.provider
    assert any(x.provider == prev.provider and x.model is None for x in plan.excluded)


@pytest.mark.parametrize("cat", [E.RATE_LIMIT, E.QUOTA_EXHAUSTED])
def test_quota_waits_when_no_alternative(router, cat):
    prev, plan = run(router, req("add a retry helper", hints=HIGH, statuses=statuses("claude")), cat)
    assert plan.action == "wait_for_quota"


def test_auth_switches_else_blocks(router):
    _, plan = run(router, req("add a retry helper", hints=HIGH), E.AUTH)
    assert plan.action == "switch"
    _, plan = run(router, req("add a retry helper", hints=HIGH, statuses=statuses("claude")), E.AUTH)
    assert plan.action == "block"


@pytest.mark.parametrize("cat", [E.NETWORK, E.TIMEOUT, E.CRASHED])
def test_transient_retries_then_switches(router, cat):
    request = req("add a retry helper", hints=HIGH)
    prev, plan = run(router, request, cat)
    assert plan.action == "retry_same" and plan.decision.model == prev.model
    _, plan = run(router, request, cat, AttemptInfo(same_candidate_retries=1, session_id="s1"))
    assert plan.action == "resume_same"
    _, plan = run(router, request, cat, AttemptInfo(same_candidate_retries=2, session_id="s1"))
    assert plan.action == "switch"
    assert plan.excluded[-1].model is not None  # only the failing model is excluded, not the provider


def test_transient_exhausted_without_alternative_blocks(routing_cfg, policies_cfg):
    router = synthetic_router(routing_cfg, policies_cfg, {"solo": [("only-frontier", "frontier")]})
    request = req("add a retry helper", hints={"complexity": "critical"}, statuses=statuses("solo"))
    prev, plan = run(router, request, E.NETWORK, AttemptInfo(same_candidate_retries=2))
    assert plan.action == "block"


def test_invalid_request_never_retries_same(router):
    prev, plan = run(router, req("add a retry helper", hints=HIGH), E.INVALID_REQUEST)
    assert plan.action == "switch"
    assert (plan.decision.provider, plan.decision.model) != (prev.provider, prev.model)


def test_permission_denied_needs_approval(router):
    _, plan = run(router, req(), E.PERMISSION_DENIED)
    assert plan.action == "needs_approval"


def test_cancelled_no_fallback(router):
    _, plan = run(router, req(), E.CANCELLED)
    assert plan.action == "fail" and plan.decision is None


def test_unknown_single_retry_then_switch(router):
    request = req("add a retry helper", hints=HIGH)
    _, plan = run(router, request, E.UNKNOWN)
    assert plan.action == "retry_same"
    _, plan = run(router, request, E.UNKNOWN, AttemptInfo(same_candidate_retries=1))
    assert plan.action == "switch"


def test_unknown_is_not_treated_as_quota(routing_cfg, policies_cfg):
    router = synthetic_router(routing_cfg, policies_cfg, {"solo": [("only-advanced", "advanced")]})
    _, plan = run(router, req("add a retry helper", hints=HIGH, statuses=statuses("solo")), E.UNKNOWN,
                  AttemptInfo(same_candidate_retries=5))
    assert plan.action == "block"


def test_switch_cap_respected(router):
    request = req("add a retry helper", hints=HIGH)
    _, plan = run(router, request, E.INVALID_REQUEST, AttemptInfo(provider_switches=3))
    assert plan.action == "block" and "switch limit" in plan.reason
    _, plan = run(router, request, E.QUOTA_EXHAUSTED, AttemptInfo(provider_switches=3))
    assert plan.action == "wait_for_quota"


def test_never_downgrades_tier(router):
    request = req("add a retry helper", hints=HIGH)
    prev, plan = run(router, request, E.QUOTA_EXHAUSTED)
    assert plan.action == "switch" and plan.decision.tier in ("advanced", "frontier")
    # leave only a standard-tier provider alive: must wait, not downgrade
    only_std = req("add a retry helper", hints=HIGH, statuses=statuses("claude", "antigravity"),
                   preferences=None or req().preferences, excluded=[
                       ExcludedCandidate(provider="antigravity", model=m, reason="x") for m in
                       ("gemini-4-argon-high", "gemini-4-argon-medium", "claude-opus-5-5-high",
                        "claude-opus-5-5-medium", "claude-opus-5-5-low")])
    prev2 = router.route(only_std)
    plan = plan_fallback(prev2, E.QUOTA_EXHAUSTED, AttemptInfo(), only_std, router)
    assert plan.action == "wait_for_quota"


def test_allow_lower_tier_config_is_honoured(routing_cfg, policies_cfg, models_cfg):
    import copy
    cfg = copy.deepcopy(routing_cfg)
    cfg["fallback"]["allow_lower_tier"] = True
    rt = Router(cfg, policies_cfg, CapabilityRegistry(models_cfg))
    request = req("add a retry helper", hints=HIGH, statuses=statuses("codex", "antigravity"),
                  excluded=[ExcludedCandidate(provider="antigravity", model=m, reason="x") for m in
                            ("gemini-4-argon-high", "gemini-4-argon-medium", "claude-opus-5-5-high",
                             "claude-opus-5-5-medium", "claude-opus-5-5-low")])
    prev = rt.route(request)
    plan = plan_fallback(prev, E.QUOTA_EXHAUSTED, AttemptInfo(), request, rt)
    assert plan.action == "switch" and plan.decision.tier == "standard"


def test_exclude_failed_candidate_false_allows_same(routing_cfg, policies_cfg, models_cfg):
    import copy
    cfg = copy.deepcopy(routing_cfg)
    cfg["fallback"]["exclude_failed_candidate"] = False
    rt = Router(cfg, policies_cfg, CapabilityRegistry(models_cfg))
    request = req("add a retry helper", hints=HIGH)
    prev = rt.route(request)
    plan = plan_fallback(prev, E.INVALID_REQUEST, AttemptInfo(), request, rt)
    assert plan.excluded == []


def test_previous_decision_without_candidate_blocks(router):
    request = req(usage=[usage(p, U.EXHAUSTED, resets_at=at(9)) for p in ("claude", "codex", "antigravity")])
    prev = router.route(request)
    assert plan_fallback(prev, E.NETWORK, AttemptInfo(), request, router).action == "block"


def test_fallback_deterministic(router):
    request = req("add a retry helper", hints=HIGH)
    a = run(router, request, E.RATE_LIMIT)[1]
    b = run(router, request, E.RATE_LIMIT)[1]
    assert a.model_dump() == b.model_dump()


# ----------------------------------------------------------------------------- adaptive (pure)
def test_adaptive_below_threshold_is_zero():
    a = compute_adjustment({"verified_runs": 19, "successes": 19}, min_samples=20, max_adjustment=10)
    assert a.points == 0 and not a.applied
    assert "insufficient evidence (n < min_samples)" in a.explanation


def test_adaptive_at_threshold_bounded_with_explanation():
    best = compute_adjustment({"verified_runs": 20, "successes": 20}, min_samples=20, max_adjustment=10)
    worst = compute_adjustment({"verified_runs": 500, "successes": 0}, min_samples=20, max_adjustment=10)
    mid = compute_adjustment({"verified_runs": 40, "successes": 20}, min_samples=20, max_adjustment=10)
    assert best.points == 10 and worst.points == -10 and mid.points == 0
    assert best.applied and "20/20" in best.explanation


def test_adaptive_missing_stats_and_disabled():
    assert compute_adjustment(None, min_samples=20, max_adjustment=10).points == 0
    d = compute_adjustment({"verified_runs": 99, "successes": 99}, min_samples=20, max_adjustment=10, enabled=False)
    assert d.points == 0 and "disabled" in d.explanation
