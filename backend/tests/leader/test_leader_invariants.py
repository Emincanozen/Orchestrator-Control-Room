"""Exactly-one-leader invariant, leader/advisor eligibility, default leader selection."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from orchestrator.errors import Conflict, ValidationFailed
from orchestrator.leader.service import LeadershipUpdate
from orchestrator.providers.mock import MockScript, MockStep

from .conftest import Brain

SPEC = {
    "alpha": {"models": [("alpha-model", "frontier")]},
    "beta": {"models": [("beta-model", "advanced")]},
    "gamma": {"models": [("gamma-model", "standard")]},  # below the leader minimum tier (advanced)
    "delta": {"models": [("delta-model", "frontier")], "caps": ["coding"]},  # no planning/review capability
    "paid": {"models": [("paid-model", "frontier")], "billing": "metered_api"},
    "listed": {"models": [("listed-model", "frontier")], "verified": False},
}


async def test_database_enforces_a_single_leader_row(lh):
    db = lh.ctx.db
    with pytest.raises(sqlite3.IntegrityError):
        await db.write(lambda tx: tx.execute("INSERT INTO leadership (id, provider, model, advisor, updated_at) VALUES (2, 'beta', 'm', '{}', 'now')"))
    with pytest.raises(sqlite3.IntegrityError):  # the row exists already
        await db.write(lambda tx: tx.execute("INSERT INTO leadership (id, provider, model, advisor, updated_at) VALUES (1, 'beta', 'm', '{}', 'now')"))
    with pytest.raises(sqlite3.DatabaseError):  # and cannot be deleted
        await db.write(lambda tx: tx.execute("DELETE FROM leadership"))
    assert await db.read(lambda tx: tx.scalar("SELECT COUNT(*) FROM leadership")) == 1


async def test_service_refuses_a_second_first_leader_and_stale_compare_and_set(lh):
    svc = lh.stack.leadership
    with pytest.raises(Conflict):
        await lh.ctx.db.write(lambda tx: svc.set_leader_tx(tx, "beta", "beta-model", source="x", reason="x", expect=None))
    with pytest.raises(Conflict):  # caller believed the leader was beta
        await lh.ctx.db.write(lambda tx: svc.set_leader_tx(tx, "beta", "beta-model", source="x", reason="x", expect=("beta", "beta-model")))
    assert await svc.leader() == ("alpha", "alpha-model")


async def test_concurrent_switches_leave_exactly_one_leader(lh):
    # the validation call of the first switch is slow, so the second one arrives while it is in progress
    lh.brain.push("switch", MockScript(steps=[MockStep(action="sleep", seconds=0.4)] + MockScript.success(
        "ok", structured_output={"tasks": []}).steps))
    a = asyncio.create_task(lh.stack.switch.switch("beta", "beta-model", reason="a"))
    await asyncio.sleep(0.1)
    with pytest.raises(Conflict) as exc:
        await lh.stack.switch.switch("beta", "beta-model", reason="b")
    assert exc.value.code == "switch_in_progress"
    res = await a
    assert res["status"] == "switched"
    assert await lh.ctx.db.read(lambda tx: tx.scalar("SELECT COUNT(*) FROM leadership")) == 1
    assert await lh.stack.leadership.leader() == ("beta", "beta-model")
    # a gather of two identical requests: one wins, the other fails; still one row
    results = await asyncio.gather(
        lh.stack.switch.switch("alpha", "alpha-model", reason="x"), lh.stack.switch.switch("alpha", "alpha-model", reason="y"),
        return_exceptions=True,
    )
    assert sum(isinstance(r, dict) for r in results) == 1 and sum(isinstance(r, Exception) for r in results) == 1
    assert await lh.ctx.db.read(lambda tx: tx.scalar("SELECT COUNT(*) FROM leadership")) == 1


async def test_ineligible_leaders_are_rejected_with_reasons(make_lh):
    h = await make_lh(spec=SPEC)
    v = h.stack.leadership.validate
    ok = await v("leader", "alpha", "alpha-model")
    assert ok["eligible"] and ok["tier"] == "frontier"
    assert (await v("leader", "beta", "beta-model"))["tier"] == "advanced"  # fallback tier of the leader role
    with pytest.raises(ValidationFailed) as e:
        await v("leader", "gamma", "gamma-model")
    assert "below_min_tier" in e.value.details["reasons"][0] or "tier standard" in e.value.details["reasons"][0]
    assert e.value.details["min_tier"] == "advanced"
    with pytest.raises(ValidationFailed) as e:
        await v("leader", "delta", "delta-model")
    assert "planning" in e.value.message
    with pytest.raises(ValidationFailed) as e:
        await v("leader", "paid", "paid-model")
    assert "billing" in e.value.message
    with pytest.raises(ValidationFailed) as e:  # unknown model
        await v("leader", "alpha", "no-such-model")
    assert "not a known model" in e.value.message
    with pytest.raises(ValidationFailed):  # provider that is not installed/registered at all
        await v("leader", "ghost", "ghost-model")
    with pytest.raises(ValidationFailed):  # a leader needs a model
        await v("leader", "alpha", None)


async def test_ineligible_advisor_is_rejected_and_advisor_never_becomes_leader(make_lh):
    h = await make_lh(spec=SPEC)
    svc = h.stack.leadership
    await svc.update(LeadershipUpdate(advisor={"provider": "beta", "model": "beta-model"}))
    assert (await svc.identities())["advisor"] == {"provider": "beta", "model": "beta-model"}
    with pytest.raises(ValidationFailed):
        await svc.update(LeadershipUpdate(advisor={"provider": "delta", "model": "delta-model"}))
    # the advisor identity is stored apart from the leader row's identity; configuring it never changes the leader
    assert await svc.leader() == ("alpha", "alpha-model")
    # advisor routing only ever carries phase 'advise'
    pin = (await h.routing.leadership_pins())
    assert pin["advisor"] == ("beta", "beta-model") and pin["leader"] == ("alpha", "alpha-model")
    await svc.update(LeadershipUpdate(advisor=None))
    assert (await svc.identities())["advisor"] is None


async def test_default_leader_is_deterministic_verified_and_never_metered(make_lh):
    h = await make_lh(spec=SPEC, leader=None)
    assert await h.stack.leadership.leader() is None
    made = await h.stack.leadership.ensure_default_leader()
    # the CHEAPEST eligible verified non-metered model: beta (advanced) beats the frontier alpha; delta lacks
    # planning, paid is metered, listed is unverified, gamma is below the minimum tier
    assert made == {"provider": "beta", "model": "beta-model"}
    ident = await h.stack.leadership.identities()
    assert ident["leader_meta"]["reason"] == "default" and ident["leader_meta"]["source"] == "default"
    assert "cheapest eligible verified model" in ident["leader_meta"]["detail"] and "beta/beta-model (advanced)" in ident["leader_meta"]["detail"]
    assert h.events("leader.changed")[0]["payload"]["detail"] == ident["leader_meta"]["detail"]
    ev = h.events("leader.changed")
    assert len(ev) == 1 and ev[0]["payload"]["reason"] == "default" and ev[0]["payload"]["from"] is None
    assert await h.stack.leadership.ensure_default_leader() == made  # idempotent
    assert len(h.events("leader.changed")) == 1
    # a second harness over the same catalogue picks the same one (deterministic)
    h2 = await make_lh(spec=SPEC, leader=None)
    assert await h2.stack.leadership.ensure_default_leader() == made


async def test_default_leader_falls_back_to_the_frontier_model_when_it_is_the_only_eligible_one(make_lh):
    h = await make_lh(spec={"alpha": {"models": [("alpha-model", "frontier")]}, "gamma": {"models": [("gamma-model", "standard")]}}, leader=None)
    assert await h.stack.leadership.ensure_default_leader() == {"provider": "alpha", "model": "alpha-model"}


async def test_leader_state_reflects_detection_default_and_none(make_lh):
    h = await make_lh(spec=SPEC, leader=None)
    svc = h.stack.leadership
    assert await svc.leader_state() == "none"
    svc.default_state = "detecting"
    assert await svc.leader_state() == "detecting"
    await svc.ensure_default_leader()
    assert await svc.leader_state() == "set" and svc.default_state == "idle"
    h2 = await make_lh(spec={"paid": {"models": [("paid-model", "frontier")], "billing": "metered_api"}}, leader=None)
    await h2.stack.leadership.ensure_default_leader()
    assert await h2.stack.leadership.leader_state() == "none" and h2.stack.leadership.default_state == "none"


async def test_no_default_when_only_metered_or_unverified_models_exist(make_lh):
    h = await make_lh(spec={"paid": {"models": [("paid-model", "frontier")], "billing": "metered_api"},
                           "listed": {"models": [("listed-model", "frontier")], "verified": False}}, leader=None)
    assert await h.stack.leadership.ensure_default_leader() is None
    assert await h.stack.leadership.leader() is None
    assert [e for e in h.events("system.notice") if "no verified" in e["payload"].get("message", "")]


async def test_settings_update_links_limits_and_validates(lh):
    svc = lh.stack.leadership
    await svc.update(LeadershipUpdate(advisor_policy="auto", fallback_policy="any_capable", max_consultations_per_day=3,
                                      max_leader_calls_per_task=4, max_consultations_per_task=1, max_parallel_workers=2))
    s = await svc.settings()
    assert (s.advisor_policy, s.fallback_policy, s.max_consultations_per_day) == ("auto", "any_capable", 3)
    limits = await svc.limits()  # linked to the policy limits, not duplicated
    assert limits["max_leader_calls_per_task"] == 4 and limits["max_consultations_per_task"] == 1 and limits["max_parallel_workers"] == 2
    assert (await lh.ctx.policies()).int_limit("max_parallel_workers") == 2
    with pytest.raises(Exception):
        LeadershipUpdate(advisor_policy="sometimes")
