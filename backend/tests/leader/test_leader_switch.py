"""Safe leader switching: protocol, rollback, paused assignment, healthy workers, automatic fallback."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from orchestrator.domain.states import TaskState as S
from orchestrator.errors import ValidationFailed
from orchestrator.leader.service import LeadershipUpdate
from orchestrator.providers.mock import MockScript
from orchestrator.usage import UsageSnapshot


async def start_hanging_worker(h, project):
    h.brain.push("execute", MockScript.hang())
    t = await h.mk_task(project.id, title="long job", acceptance_criteria=["c"], plan={"x": 1},
                        verify_commands=[["python", "-c", "print(1)"]])
    await h.tasks.transition(t.id, S.QUEUED, reason="s")
    runner = asyncio.create_task(h.run_once(t.id))
    for _ in range(200):
        r = await h.ctx.db.read(lambda tx: tx.one("SELECT * FROM runs WHERE task_id = ? AND phase = 'execute'", (t.id,)))
        if r and r["status"] == "running":
            return t.id, runner
        await asyncio.sleep(0.05)
    raise AssertionError("worker did not start")


async def test_happy_path_keeps_a_hanging_worker_running(lh):
    p = await lh.project()
    tid, worker = await start_hanging_worker(lh, p)
    seen = {}
    lh.brain.hooks["switch"] = lambda req: seen.setdefault("paused", lh.scheduler.assignment_paused)
    res = await lh.stack.switch.switch("beta", "beta-model", reason="test")
    assert res["status"] == "switched" and res["validated"] and res["attempts"] == 1
    assert await lh.stack.leadership.leader() == ("beta", "beta-model")
    assert seen["paused"] is True and lh.scheduler.assignment_paused is False  # paused during, resumed after
    # the healthy worker was not touched
    run = await lh.ctx.db.read(lambda tx: tx.one("SELECT status FROM runs WHERE task_id = ? AND phase = 'execute'", (tid,)))
    assert run["status"] == "running" and not worker.done() and lh.providers["alpha"].cancelled == [] and lh.providers["beta"].cancelled == []
    assert (await lh.tasks.get(tid)).state == S.RUNNING
    # checkpoint: kind leadership, no task, concise structure incl. the active task
    cp = await lh.ctx.db.read(lambda tx: tx.one("SELECT * FROM checkpoints WHERE id = ?", (res["checkpoint_id"],)))
    import json
    summary = json.loads(cp["summary"])
    assert cp["kind"] == "leadership" and cp["task_id"] is None
    assert [t["id"] for t in summary["tasks"]] == [tid] and summary["tasks"][0]["state"] == "RUNNING"
    assert {"open_approvals", "open_objectives", "recent_decisions", "live_runs"} <= set(summary)
    assert summary["live_runs"][0]["task_id"] == tid
    # leader.changed
    [ev] = h_events(lh)
    assert ev["payload"]["status"] == "switched" and ev["payload"]["from"] == {"provider": "alpha", "model": "alpha-model"}
    assert ev["payload"]["to"] == {"provider": "beta", "model": "beta-model"} and ev["payload"]["checkpoint_id"] == res["checkpoint_id"]
    # the new leader really answered the validation call (pinned), on the leader seat
    assert ("beta", "switch") in lh.brain.calls
    await lh.executor.request_stop(tid, "cancel")
    await worker


def h_events(h):
    return [e for e in h.events("leader.changed")]


async def test_validation_mismatch_rolls_back_to_the_previous_leader(lh):
    p = await lh.project()
    t = await lh.mk_task(p.id, title="pending")
    await lh.tasks.transition(t.id, S.QUEUED, reason="s")
    wrong = {"tasks": [{"id": "tsk_invented", "next_action": "x"}]}
    lh.brain.push("switch", wrong, wrong)
    res = await lh.stack.switch.switch("beta", "beta-model")
    assert res["status"] == "rolled_back" and res["attempts"] == 2 and "state mismatch" in res["reason"]
    assert f"missing ['{t.id}']" in res["reason"] and "tsk_invented" in res["reason"]
    assert await lh.stack.leadership.leader() == ("alpha", "alpha-model")
    assert lh.scheduler.assignment_paused is False and lh.stack.leadership.switch_in_progress is False
    ev = h_events(lh)[-1]["payload"]
    assert ev["status"] == "rolled_back" and "state mismatch" in ev["rollback_reason"]
    assert "rejected" in lh.brain.prompts["switch"][1]  # the 2nd attempt was told what was wrong


async def test_second_attempt_can_succeed(lh):
    p = await lh.project()
    t = await lh.mk_task(p.id, title="pending")
    await lh.tasks.transition(t.id, S.QUEUED, reason="s")
    lh.brain.push("switch", {"tasks": []})
    res = await lh.stack.switch.switch("beta", "beta-model")
    assert res["status"] == "switched" and res["attempts"] == 2


async def test_assignment_is_resumed_even_when_an_exception_occurs(lh, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("validation exploded")

    monkeypatch.setattr(lh.stack.switch, "_validate", boom)
    with pytest.raises(RuntimeError):
        await lh.stack.switch.switch("beta", "beta-model")
    assert lh.scheduler.assignment_paused is False and lh.stack.leadership.switch_in_progress is False
    assert await lh.stack.leadership.leader() == ("alpha", "alpha-model")  # and the half-set leader was rolled back
    assert h_events(lh)[-1]["payload"]["status"] == "rolled_back"


async def test_assignment_paused_means_the_scheduler_starts_nothing(lh):
    p = await lh.project()
    t = await lh.mk_task(p.id, title="q", acceptance_criteria=["c"], plan={"x": 1}, verify_commands=[["python", "-c", "print(1)"]])
    await lh.tasks.transition(t.id, S.QUEUED, reason="s")
    started_during = []

    def hook(req):
        started_during.append(asyncio.get_event_loop().create_task(lh.scheduler.tick()))

    lh.brain.hooks["switch"] = hook
    await lh.stack.switch.switch("beta", "beta-model")
    assert await started_during[0] == []  # tick during the switch assigned nothing
    assert await lh.scheduler.tick() == [t.id]  # after the switch it does


async def test_skip_validation_is_local_only_and_recorded(make_lh):
    spec = {"alpha": {"models": [("alpha-model", "frontier")]}, "beta": {"models": [("beta-model", "frontier")]},
            "claude": {"models": [("claude-opus", "advanced")], "billing": "subscription_cli"}}
    h = await make_lh(spec=spec)
    with pytest.raises(ValidationFailed):
        await h.stack.switch.switch("claude", "claude-opus", skip_validation=True)
    assert await h.stack.leadership.leader() == ("alpha", "alpha-model")
    res = await h.stack.switch.switch("beta", "beta-model", skip_validation=True)
    assert res["status"] == "switched" and res["skip_validation"] is True and res["validated"] is False
    assert ("beta", "switch") not in h.brain.calls
    assert h_events(h)[-1]["payload"]["skip_validation"] is True


async def test_switch_rejects_same_or_ineligible_target(lh):
    with pytest.raises(ValidationFailed):
        await lh.stack.switch.switch("alpha", "alpha-model")
    with pytest.raises(ValidationFailed):
        await lh.stack.switch.switch("beta", "nope")


# ------------------------------------------------------------------ automatic fallback
async def exhaust(h, provider, model=None):
    snap = UsageSnapshot(provider=provider, model=model, state="EXHAUSTED", metric="rate_limit", exactness="exact",
                         source="test", confidence="high", observed_at=datetime.now(UTC))
    from orchestrator.domain.states import UsageState

    snap = snap.model_copy(update={"state": UsageState.EXHAUSTED})
    await h.usage.record([snap], provider=provider, cause="test")


async def test_automatic_fallback_uses_the_same_protocol(lh):
    await lh.stack.leadership.update(LeadershipUpdate(fallback_policy="any_capable"))
    assert await lh.stack.switch.auto_fallback() is None  # leader healthy: nothing happens
    await exhaust(lh, "alpha")
    res = await lh.stack.switch.auto_fallback()
    assert res["status"] == "switched" and res["to"]["provider"] == "beta"
    ev = h_events(lh)[-1]["payload"]
    assert ev["source"] == "fallback" and "EXHAUSTED" in ev["reason"] and ev["validated"] is True and ev["checkpoint_id"]


async def test_fallback_policy_off_does_nothing(lh):
    await exhaust(lh, "alpha")
    assert await lh.stack.switch.auto_fallback() is None
    assert await lh.stack.leadership.leader() == ("alpha", "alpha-model")


async def test_fallback_is_triggered_by_the_next_leader_call_and_respects_min_tier(make_lh):
    spec = {"alpha": {"models": [("alpha-model", "frontier")]}, "gamma": {"models": [("gamma-model", "standard")]}}
    h = await make_lh(spec=spec)
    await h.stack.leadership.update(LeadershipUpdate(fallback_policy="any_capable"))
    await exhaust(h, "alpha")
    # the only other model is below the leader role's minimum tier: never picked
    assert await h.stack.switch.auto_fallback() is None
    assert await h.stack.leadership.leader() == ("alpha", "alpha-model")
    assert [e for e in h.events("system.notice") if "no eligible fallback" in e["payload"].get("message", "")]


async def test_same_provider_lower_tier_fires_for_a_model_scoped_exhaustion_and_never_crosses_providers(make_lh):
    spec = {"alpha": {"models": [("alpha-top", "frontier"), ("alpha-mid", "advanced")]}, "beta": {"models": [("beta-model", "frontier")]}}
    h = await make_lh(spec=spec, leader=("alpha", "alpha-top"))
    await h.stack.leadership.update(LeadershipUpdate(fallback_policy="same_provider_lower_tier"))
    await exhaust(h, "alpha", "alpha-top")  # model-scoped: only alpha-top is blocked, alpha-mid is still usable
    res = await h.stack.switch.auto_fallback()
    assert res["status"] == "switched" and res["to"] == {"provider": "alpha", "model": "alpha-mid"}


async def test_same_provider_lower_tier_refuses_beta_when_the_whole_provider_is_exhausted(make_lh):
    spec = {"alpha": {"models": [("alpha-top", "frontier"), ("alpha-mid", "advanced")]}, "beta": {"models": [("beta-model", "frontier")]}}
    h = await make_lh(spec=spec, leader=("alpha", "alpha-top"))
    await h.stack.leadership.update(LeadershipUpdate(fallback_policy="same_provider_lower_tier"))
    await exhaust(h, "alpha")  # provider-scoped (account-level): every alpha model is blocked
    assert await h.stack.switch.auto_fallback() is None
    assert await h.stack.leadership.leader() == ("alpha", "alpha-top")
    await h.stack.leadership.update(LeadershipUpdate(fallback_policy="any_capable"))
    res = await h.stack.switch.auto_fallback()
    assert res["status"] == "switched" and res["to"]["provider"] == "beta"
