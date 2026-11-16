"""Usage snapshots and state derivation (ADR-009)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from orchestrator.domain.states import UsageState as S
from orchestrator.providers.base import (
    AgentEvent,
    ExecutionResult,
    ProviderStatus,
)
from orchestrator.providers.base import (
    AgentEventKind as K,
)
from orchestrator.providers.base import (
    ErrorCategory as C,
)
from orchestrator.usage import derive_state, snapshot_from_status, snapshots_from_event, snapshots_from_result

T0 = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
RESET = int((T0 + timedelta(hours=3)).timestamp())


def rl(status="allowed", util=0.06, resets=RESET, window="five_hour"):
    return AgentEvent(kind=K.RATE_LIMIT, data={"status": status, "utilization": util, "resets_at": resets, "window": window})


def state(obs, now, **kw):
    return derive_state(obs, provider="claude", now=now, **kw)


def test_utilization_snapshot_fields():
    snaps = snapshots_from_event("claude", rl(), observed_at=T0)
    util = next(s for s in snaps if s.metric == "window_utilization.five_hour")
    assert (util.value, util.unit, util.exactness, util.confidence) == (0.06, "ratio", "exact", "high")
    assert util.source == "claude.rate_limit_event" and util.state == S.AVAILABLE
    assert util.resets_at == datetime.fromtimestamp(RESET, tz=UTC) and util.observed_at == T0


def test_low_threshold():
    assert snapshots_from_event("claude", rl(util=0.79), observed_at=T0)[0].state == S.AVAILABLE
    assert snapshots_from_event("claude", rl(util=0.8), observed_at=T0)[0].state == S.LOW
    assert state(snapshots_from_event("claude", rl(util=0.85), observed_at=T0), T0 + timedelta(minutes=5)).state == S.LOW


def test_no_utilization_means_no_fabricated_percentage():
    snaps = snapshots_from_event("claude", rl(util=None), observed_at=T0)
    assert all(not s.metric.startswith("window_utilization") for s in snaps)
    assert all(s.value is None for s in snaps)


def test_rejected_is_exhausted_until_reset_then_unknown():
    snaps = snapshots_from_event("claude", rl(status="rejected", util=1.0), observed_at=T0)
    assert state(snaps, T0 + timedelta(hours=1)).state == S.EXHAUSTED
    st = state(snaps, T0 + timedelta(hours=3, seconds=1))
    assert st.state == S.UNKNOWN  # never AVAILABLE until re-observed
    reobserved = snaps + snapshots_from_event("claude", rl(util=0.01, resets=RESET + 18000), observed_at=T0 + timedelta(hours=3, minutes=5))
    assert state(reobserved, T0 + timedelta(hours=3, minutes=6)).state == S.AVAILABLE


def test_later_observation_replaces_earlier():
    a = snapshots_from_event("claude", rl(status="rejected", util=1.0), observed_at=T0)
    b = snapshots_from_event("claude", rl(status="allowed", util=0.1), observed_at=T0 + timedelta(minutes=10))
    assert state(a + b, T0 + timedelta(minutes=11)).state == S.AVAILABLE


def test_quota_error_exhausted_then_unknown_after_recheck():
    res = ExecutionResult(success=False, error_category=C.QUOTA_EXHAUSTED, model="haiku")
    snaps = snapshots_from_result("codex", res, observed_at=T0)
    st = derive_state(snaps, provider="codex", now=T0 + timedelta(seconds=60))
    assert st.state == S.EXHAUSTED and st.resets_at is None
    assert derive_state(snaps, provider="codex", now=T0 + timedelta(seconds=301)).state == S.UNKNOWN
    with_reset = snapshots_from_result("codex", res, observed_at=T0, retry_after=RESET)
    assert derive_state(with_reset, provider="codex", now=T0 + timedelta(hours=2)).state == S.EXHAUSTED


def test_rate_limit_error_is_cooldown():
    snaps = snapshots_from_result("claude", ExecutionResult(success=False, error_category=C.RATE_LIMIT), observed_at=T0)
    assert state(snaps, T0 + timedelta(seconds=30)).state == S.COOLDOWN


def test_auth_error_is_sticky_until_success_or_auth_ok():
    snaps = snapshots_from_result("claude", ExecutionResult(success=False, error_category=C.AUTH), observed_at=T0)
    assert state(snaps, T0 + timedelta(days=3)).state == S.AUTH_ERROR
    ok = snapshot_from_status(ProviderStatus(provider="claude", installed=True, authenticated=True),
                              observed_at=T0 + timedelta(hours=1))
    assert state(snaps + ok, T0 + timedelta(hours=2)).state == S.UNKNOWN
    success = snapshots_from_result("claude", ExecutionResult(success=True), observed_at=T0 + timedelta(hours=1))
    assert state(snaps + success, T0 + timedelta(hours=2)).state == S.AVAILABLE


def test_success_after_exhaustion_disproves_it_but_not_before():
    exhausted = snapshots_from_event("claude", rl(status="rejected", util=1.0), observed_at=T0)
    success = snapshots_from_result("claude", ExecutionResult(success=True), observed_at=T0 + timedelta(minutes=5))
    assert state(exhausted + success, T0 + timedelta(minutes=6)).state == S.AVAILABLE
    success_first = snapshots_from_result("claude", ExecutionResult(success=True), observed_at=T0 - timedelta(minutes=5))
    assert state(success_first + exhausted, T0 + timedelta(minutes=1)).state == S.EXHAUSTED


def test_worst_window_wins():
    obs = snapshots_from_event("claude", rl(util=0.2, window="five_hour"), observed_at=T0)
    obs += snapshots_from_event("claude", rl(util=0.95, window="seven_day"), observed_at=T0)
    assert state(obs, T0 + timedelta(minutes=1)).state == S.LOW


def test_no_observation_is_unknown():
    st = state([], T0)
    assert st.state == S.UNKNOWN
    old_success = snapshots_from_result("claude", ExecutionResult(success=True), observed_at=T0)
    assert state(old_success, T0 + timedelta(days=2)).state == S.UNKNOWN


def test_other_provider_and_model_scoping():
    # evidence that names its model is model-scoped
    codex_exhausted = snapshots_from_result("codex", ExecutionResult(success=False, error_category=C.QUOTA_EXHAUSTED,
                                                                     model="m1-x", error_message="quota exhausted for m1-x"), observed_at=T0)
    assert derive_state(codex_exhausted, provider="claude", now=T0).state == S.UNKNOWN
    assert derive_state(codex_exhausted, provider="codex", model="m2", now=T0).state == S.UNKNOWN  # model-scoped observation
    assert derive_state(codex_exhausted, provider="codex", model="m1-x", now=T0).state == S.EXHAUSTED
    assert derive_state(codex_exhausted, provider="codex", now=T0).state == S.UNKNOWN
    # without the model in the error text the evidence is provider-scoped (account-level), whatever model ran
    account = snapshots_from_result("codex", ExecutionResult(success=False, error_category=C.QUOTA_EXHAUSTED, model="m1"), observed_at=T0)
    assert derive_state(account, provider="codex", model="m2", now=T0).state == S.EXHAUSTED
    assert derive_state(account, provider="codex", now=T0).state == S.EXHAUSTED


def test_non_provider_errors_leave_state_alone():
    for cat in (C.INVALID_REQUEST, C.TOOL_FAILURE, C.PERMISSION_DENIED, C.CANCELLED, C.UNKNOWN, C.TIMEOUT):
        snaps = snapshots_from_result("claude", ExecutionResult(success=False, error_category=cat), observed_at=T0)
        assert state(snaps, T0).state == S.UNKNOWN, cat


def test_cost_is_an_estimate_and_tokens_exact():
    res = ExecutionResult(success=True, model="haiku", usage={
        "input_tokens": 2, "output_tokens": 11, "cached_input_tokens": 649, "cost_usd": 0.00085899, "cost_basis": "list_price_equivalent"})
    snaps = {s.metric: s for s in snapshots_from_result("claude", res, observed_at=T0)}
    assert snaps["cost_usd"].exactness == "estimate" and snaps["cost_usd"].unit == "usd_list_price_equivalent"
    assert snaps["input_tokens"].exactness == "exact" and snaps["input_tokens"].value == 2.0
    assert "cost_basis" not in snaps and snaps["cached_input_tokens"].value == 649.0


def test_status_snapshots():
    assert snapshot_from_status(ProviderStatus(provider="agy", installed=False))[0].state == S.PROVIDER_ERROR
    assert snapshot_from_status(ProviderStatus(provider="agy", installed=True, authenticated=False))[0].state == S.AUTH_ERROR
    assert snapshot_from_status(ProviderStatus(provider="agy", installed=True, authenticated=None)) == []


# --------------------------------------------------------------------------- M9a: model-scoped vs provider-scoped evidence
def _fail(category, message, model="m-top"):
    return ExecutionResult(success=False, error_category=category, error_message=message, model=model)


def test_error_scope_is_model_only_when_the_error_names_the_model():
    from orchestrator.usage import snapshots as sn

    assert sn.error_scope(_fail(C.QUOTA_EXHAUSTED, "quota exceeded for model m-top")) == "model"
    assert sn.error_scope(_fail(C.RATE_LIMIT, "429 on M-TOP, slow down")) == "model"
    assert sn.error_scope(_fail(C.QUOTA_EXHAUSTED, "You've hit your usage limit")) == "provider"  # account-level wording
    assert sn.error_scope(_fail(C.AUTH, "auth failed for m-top")) == "provider"  # auth is always provider-scoped
    assert sn.error_scope(_fail(C.NETWORK, "m-top unreachable")) == "provider"
    assert sn.error_scope(_fail(C.QUOTA_EXHAUSTED, "quota", model=None)) == "provider"


def test_model_scoped_quota_marks_only_that_model():
    snaps = snapshots_from_result("p", _fail(C.QUOTA_EXHAUSTED, "quota exhausted for m-top"), observed_at=T0)
    err = [s for s in snaps if s.metric == "provider_error"]
    assert err and err[0].model == "m-top"
    now = T0 + timedelta(seconds=30)
    assert derive_state(snaps, provider="p", model="m-top", now=now).state == S.EXHAUSTED
    assert derive_state(snaps, provider="p", model="m-lite", now=now).state == S.UNKNOWN  # sibling untouched
    assert derive_state(snaps, provider="p", now=now).state == S.UNKNOWN  # provider-wide view untouched


def test_provider_scoped_observations_mark_every_model():
    quota = snapshots_from_result("p", _fail(C.QUOTA_EXHAUSTED, "You've hit your usage limit"), observed_at=T0)
    assert [s.model for s in quota if s.metric == "provider_error"] == [None]
    auth = snapshots_from_result("p", _fail(C.AUTH, "not logged in"), observed_at=T0)
    claude_rl = snapshots_from_event("p", rl(status="rejected", util=1.0), observed_at=T0)
    now = T0 + timedelta(seconds=30)
    for obs, want in ((quota, S.EXHAUSTED), (auth, S.AUTH_ERROR), (claude_rl, S.EXHAUSTED)):
        for model in (None, "m-top", "m-lite"):
            assert derive_state(obs, provider="p", model=model, now=now).state == want


def test_success_of_the_model_disproves_its_own_scoped_exhaustion_only():
    bad = snapshots_from_result("p", _fail(C.QUOTA_EXHAUSTED, "quota for m-top"), observed_at=T0)
    ok_top = snapshots_from_result("p", ExecutionResult(success=True, model="m-top"), observed_at=T0 + timedelta(seconds=5))
    ok_lite = snapshots_from_result("p", ExecutionResult(success=True, model="m-lite"), observed_at=T0 + timedelta(seconds=5))
    now = T0 + timedelta(seconds=30)
    assert derive_state(bad + ok_top, provider="p", model="m-top", now=now).state == S.AVAILABLE
    assert derive_state(bad + ok_lite, provider="p", model="m-top", now=now).state == S.EXHAUSTED
