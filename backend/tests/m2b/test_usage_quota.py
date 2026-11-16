"""Usage monitor: snapshots from events/results, derived state, scheduler quota_check."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from orchestrator.db import records
from orchestrator.domain.states import TaskState, UsageState
from orchestrator.providers.base import AgentEvent, AgentEventKind, ErrorCategory, ExecutionResult
from orchestrator.providers.mock import MockScript, MockStep

from .conftest import OK

S = TaskState


def _future(hours=2) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).replace(microsecond=0).isoformat()


async def _rows(h):
    return await h.ctx.db.all("SELECT * FROM usage_snapshots ORDER BY observed_at, rowid")


async def test_rate_limit_and_usage_events_become_snapshots(rh):
    reset = _future(3)
    await rh.usage.on_event("alpha", "alpha-model", AgentEvent(
        kind=AgentEventKind.RATE_LIMIT, data={"status": "allowed_warning", "utilization": 0.85, "resets_at": reset, "window": "five_hour"}))
    await rh.usage.on_event("alpha", "alpha-model", AgentEvent(kind=AgentEventKind.USAGE, data={"input_tokens": 120, "output_tokens": 30, "cost_usd": 0.01}))
    rows = {(r["metric"]): r for r in await _rows(rh)}
    util = rows["window_utilization.five_hour"]
    assert util["value"] == 0.85 and util["exactness"] == "exact" and util["state"] == "LOW"
    assert util["source"] == "alpha.rate_limit_event" and util["resets_at"] is not None
    assert rows["input_tokens"]["exactness"] == "exact" and rows["input_tokens"]["state"] == "UNKNOWN"
    assert rows["cost_usd"]["exactness"] == "estimated"  # list-price equivalent, never claimed exact
    events = await rh.ctx.bus.replay(0, types=["usage.updated"])
    assert events and events[-1].payload["provider"] == "alpha" and events[-1].payload["usage_state"]["state"] == "LOW"
    assert (await rh.usage.state_for("alpha")).state == UsageState.LOW
    assert (await rh.usage.state_for("beta")).state == UsageState.UNKNOWN  # no evidence: stays UNKNOWN


async def test_executor_forwards_events_and_results(rh):
    p = await rh.project()
    rh.mock.queue(MockScript(steps=[
        MockStep(action="rate_limit", data={"status": "allowed", "utilization": 0.1, "window": "five_hour", "resets_at": _future()}),
        MockStep(action="usage", data={"input_tokens": 7, "output_tokens": 3}),
        MockStep(action="succeed", text="ok"),
    ]))
    t = await rh.task(p.id, preferred_provider=None, acceptance_criteria=["x"], verify_commands=OK, autostart=True,
                      title="implement the repository layer")
    assert await rh.run_once(t.id) == S.COMPLETED
    metrics = {r["metric"] for r in await _rows(rh)}
    assert {"window_utilization.five_hour", "rate_limit_status", "input_tokens", "run_success"} <= metrics
    assert (await rh.usage.state_for("alpha")).state == UsageState.AVAILABLE


async def test_quota_check_resumes_only_when_state_changes(make_rh):
    h = await make_rh(("alpha",))
    p = await h.project()
    h.providers["alpha"].queue(MockScript.failure(ErrorCategory.QUOTA_EXHAUSTED, "limit", resets_at=_future()))
    t = await h.task(p.id, preferred_provider=None, acceptance_criteria=["x"], verify_commands=OK, autostart=True,
                     title="implement the repository layer")
    assert await h.run_once(t.id) == S.WAITING_FOR_QUOTA
    waiting = await h.tasks.get(t.id)
    assert (await h.usage.state_for("alpha")).state == UsageState.EXHAUSTED
    assert await h.usage.quota_check(waiting) is False

    await h.tasks.set_fields(t.id, {"not_before": None})  # the timer is not what is under test
    await h.scheduler.tick()
    assert (await h.tasks.get(t.id)).state == S.WAITING_FOR_QUOTA  # exhausted: stays

    # new evidence: a successful run on the provider (e.g. a manual run / re-detect) disproves the exhaustion
    await h.usage.on_result("alpha", ExecutionResult(success=True, model="alpha-model"))
    assert await h.usage.quota_check(waiting) is True
    await h.scheduler.tick()
    done = await h.wait_state(t.id, {S.COMPLETED})
    assert done.state == S.COMPLETED


async def test_quota_check_resumes_when_router_finds_another_candidate(make_rh):
    h = await make_rh(("alpha", "beta"), max_switches=0)  # no switching: alpha's quota error parks the task
    p = await h.project()
    h.providers["alpha"].queue(MockScript.failure(ErrorCategory.QUOTA_EXHAUSTED, "limit", resets_at=_future()))
    t = await h.task(p.id, preferred_provider=None, acceptance_criteria=["x"], verify_commands=OK, autostart=True,
                     title="implement the repository layer")
    assert await h.run_once(t.id) == S.WAITING_FOR_QUOTA
    waiting = await h.tasks.get(t.id)
    assert (await h.usage.state_for("alpha")).state == UsageState.EXHAUSTED
    # alpha is still exhausted, but beta can take the task: allowed to leave WAITING_FOR_QUOTA
    assert await h.usage.quota_check(waiting) is True
    # ... and when beta is gone too, it is not
    await h.usage.on_result("beta", ExecutionResult(success=False, error_category=ErrorCategory.QUOTA_EXHAUSTED, model="beta-model"),
                            resets_at=_future())
    assert await h.usage.quota_check(waiting) is False


async def test_auth_error_snapshot_from_detection_blocks_routing_until_success(rh):
    from orchestrator.providers.base import ProviderStatus

    await rh.usage.on_status(ProviderStatus(provider="alpha", installed=True, authenticated=False))
    assert (await rh.usage.state_for("alpha")).state == UsageState.AUTH_ERROR
    p = await rh.project()
    t = await rh.task(p.id, preferred_provider=None, acceptance_criteria=["x"], verify_commands=OK, autostart=True,
                      title="implement the repository layer")
    assert await rh.run_once(t.id) == S.COMPLETED
    [d] = await rh.ctx.db.read(lambda tx: records.routing_for_task(tx, t.id))
    assert d.chosen_provider == "beta"
    assert [(r["provider"], r["filter"]) for r in d.detail["decision"]["rejected"]] == [("alpha", "usage_auth_error")]
