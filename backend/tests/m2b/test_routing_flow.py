"""RoutingService + executor: rule-based choice, persisted decisions, no-candidate mapping, fallback."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from orchestrator.db import records, telemetry
from orchestrator.db import runs as runs_repo
from orchestrator.domain.states import TaskState
from orchestrator.providers.base import ErrorCategory, ExecutionResult
from orchestrator.providers.mock import MockScript
from orchestrator.services.routing import PREFERENCES_KEY

from .conftest import OK

S = TaskState


async def _decisions(h, task_id):
    return await h.ctx.db.read(lambda tx: records.routing_for_task(tx, task_id))


async def _task(h, p, **kw):
    kw.setdefault("preferred_provider", None)
    kw.setdefault("acceptance_criteria", ["layer works"])
    kw.setdefault("verify_commands", OK)
    kw.setdefault("title", "implement the repository layer")
    return await h.task(p.id, autostart=True, **kw)


async def test_routes_by_rules_and_persists_full_decision(rh):
    p = await rh.project()
    t = await _task(rh, p)
    assert await rh.run_once(t.id) == S.COMPLETED
    [d] = await _decisions(rh, t.id)
    assert d.chosen_provider == "alpha" and d.chosen_model == "alpha-model"  # provider_order tie-break
    assert [c["provider"] for c in d.candidates] == ["alpha", "beta"]
    top = d.candidates[0]
    assert top["rank"] == 1 and top["score"] >= d.candidates[1]["score"]
    assert set(top["components"]) == {"tier_fit", "user_preference", "context_continuity", "usage_headroom", "historical_success"}
    assert any(r.startswith("role:") for r in d.rule_ids) and d.reasons
    assert d.detail["classification"]["task_type"] == "coding"
    assert d.detail["decision"]["role"] == "backend" and d.detail["decision"]["min_tier"] == "standard"
    assert d.detail["decision"]["rejected"] == []
    events = await rh.ctx.bus.replay(0)
    assert any(e.type == "router.decision" and e.payload["provider"] == "alpha" for e in events)


async def test_task_preference_and_disabled_provider_are_honoured(rh):
    p = await rh.project()
    t = await _task(rh, p, preferred_provider="beta")
    assert await rh.run_once(t.id) == S.COMPLETED
    [d] = await _decisions(rh, t.id)
    assert d.chosen_provider == "beta"

    await rh.ctx.db.write(lambda tx: records.setting_put(tx, PREFERENCES_KEY, {"disabled_providers": ["beta"]}))
    t2 = await _task(rh, p, preferred_provider="beta", title="implement the cache layer")
    assert await rh.run_once(t2.id) == S.COMPLETED
    [d2] = await _decisions(rh, t2.id)
    assert d2.chosen_provider == "alpha"
    rej = d2.detail["decision"]["rejected"]
    assert [(r["provider"], r["filter"]) for r in rej] == [("beta", "disabled")]


async def test_no_candidate_wait_for_quota(rh):
    p = await rh.project()
    reset = (datetime.now(UTC) + timedelta(hours=2)).replace(microsecond=0)
    for name in ("alpha", "beta"):
        await rh.usage.on_result(
            name, ExecutionResult(success=False, error_category=ErrorCategory.QUOTA_EXHAUSTED, model=f"{name}-model"),
            resets_at=reset.isoformat(),
        )
    t = await _task(rh, p)
    assert await rh.run_once(t.id) == S.WAITING_FOR_QUOTA
    task = await rh.tasks.get(t.id)
    assert task.not_before is not None
    assert abs((datetime.fromisoformat(task.not_before) - reset).total_seconds()) < 1  # the known reset time
    assert "usage state blocks routing" in (task.state_reason or "")
    assert rh.mock.requests == []  # nothing was run


async def test_no_candidate_blocked_with_router_reason(rh):
    p = await rh.project()
    t = await _task(rh, p, title="generate an image logo for the app")  # no provider has image_generation
    assert await rh.run_once(t.id) == S.BLOCKED
    task = await rh.tasks.get(t.id)
    assert "no suitable candidate" in task.state_reason and "missing_capability" in task.state_reason
    assert rh.mock.requests == [] and rh.providers["beta"].requests == []


async def test_critical_risk_needs_approval_then_runs(rh):
    p = await rh.project()
    t = await _task(rh, p, title="force push the release branch")  # risk: critical
    assert await rh.run_once(t.id) == S.NEEDS_APPROVAL
    [a] = await rh.approvals.list("pending", t.id)
    assert a.kind == "high_risk_task" and "critical risk" in a.summary
    assert rh.mock.requests == []
    await rh.approvals.resolve(a.id, True)
    assert await rh.run_once(t.id) == S.COMPLETED  # approved: the gate does not loop


async def test_no_candidate_and_critical_without_capacity_needs_approval(rh):
    """requires_approval wins over a plain block when nothing is routable."""
    p = await rh.project()
    t = await _task(rh, p, title="force push the generated image logo")
    assert await rh.run_once(t.id) == S.NEEDS_APPROVAL


async def test_quota_error_switches_provider_with_handoff_checkpoint(rh):
    p = await rh.project()
    alpha, beta = rh.providers["alpha"], rh.providers["beta"]
    alpha.queue(MockScript.failure(ErrorCategory.QUOTA_EXHAUSTED, "usage limit reached", resets_at="2999-01-01T00:00:00Z"))
    beta.queue(MockScript.success("finished on beta", files={"out.txt": "1"}))
    t = await _task(rh, p)
    assert await rh.run_once(t.id) == S.QUEUED  # no waiting: an alternative exists
    mid = await rh.tasks.get(t.id)
    assert mid.not_before is None and mid.provider_switches == 1
    assert [(e["provider"], e["model"]) for e in mid.route_exclusions] == [("alpha", None)]
    cps = await rh.ctx.db.read(lambda tx: records.checkpoints_for_task(tx, t.id))
    assert [c.kind for c in cps] == ["handoff"] and "alpha -> beta" in cps[0].summary["label"]
    assert alpha.requests and len(alpha.requests) == 1

    assert await rh.run_once(t.id) == S.COMPLETED
    runs = await rh.ctx.db.read(lambda tx: runs_repo.for_task(tx, t.id))
    assert [(r.provider, r.status.value) for r in runs] == [("alpha", "failed"), ("beta", "succeeded")]
    assert len({r.idempotency_key for r in runs}) == 2  # new run, new key
    assert len(alpha.requests) == 1 and len(beta.requests) == 1  # nothing duplicated
    assert alpha.requests[0].cwd == beta.requests[0].cwd == (await rh.tasks.get(t.id)).worktree_path  # same worktree
    d = await _decisions(rh, t.id)
    assert [x.chosen_provider for x in d] == ["alpha", "beta"]
    assert d[1].detail["fallback"]["action"] == "switch" and "quota_exhausted" in d[1].reasons[0]
    assert [r["provider"] for r in d[1].detail["decision"]["rejected"]] == ["alpha"]
    # adaptive history definition: alpha's failed run belongs to a task that later passed verification
    stats = await rh.ctx.db.read(telemetry.history_stats)
    assert stats[("alpha", "alpha-model", "coding")] == {"verified_runs": 1, "successes": 0}
    assert stats[("beta", "beta-model", "coding")] == {"verified_runs": 1, "successes": 1}


async def test_max_provider_switches_is_honoured(make_rh):
    h = await make_rh(("alpha", "beta", "gamma"), max_switches=1)
    p = await h.project()
    quota = MockScript.failure(ErrorCategory.QUOTA_EXHAUSTED, "usage limit reached")
    h.providers["alpha"].queue(quota)
    h.providers["beta"].queue(MockScript.failure(ErrorCategory.QUOTA_EXHAUSTED, "usage limit reached"))
    t = await _task(h, p)
    assert await h.run_once(t.id) == S.QUEUED  # alpha -> beta (switch 1 of 1)
    assert await h.run_once(t.id) == S.WAITING_FOR_QUOTA  # beta fails; cap reached, gamma is NOT used
    task = await h.tasks.get(t.id)
    assert task.provider_switches == 1 and h.providers["gamma"].requests == []
    assert "quota_exhausted" in task.state_reason


async def test_transient_failure_retries_same_provider_without_switching(rh):
    p = await rh.project()
    alpha = rh.providers["alpha"]
    alpha.queue(MockScript.crash(), MockScript.success())
    t = await _task(rh, p)
    assert await rh.run_once(t.id) == S.QUEUED
    assert await rh.run_once(t.id) == S.COMPLETED
    task = await rh.tasks.get(t.id)
    assert task.provider_switches == 0 and len(alpha.requests) == 2 and rh.providers["beta"].requests == []
    d = await _decisions(rh, t.id)
    assert d[1].rule_ids == ["fallback.retry_same"]  # pinned: not re-filtered by the crash's PROVIDER_ERROR


async def test_retry_resumes_stored_session_when_adapter_supports_it(make_rh):
    h = await make_rh(("alpha",), resumable=("alpha",))
    p = await h.project()
    alpha = h.providers["alpha"]
    alpha.queue(MockScript.crash(), MockScript.success())
    t = await _task(h, p)
    await h.run_once(t.id)
    first = alpha.requests[0]
    assert await h.run_once(t.id) == S.COMPLETED
    assert first.resume_session_id is None
    assert alpha.requests[1].resume_session_id == f"mock-session-{first.run_id}"
    assert (await _decisions(h, t.id))[1].rule_ids == ["fallback.resume_same"]


async def test_plain_adapter_never_gets_a_resume_id(rh):
    p = await rh.project()
    alpha = rh.providers["alpha"]
    alpha.queue(MockScript.crash(), MockScript.success())
    t = await _task(rh, p)
    await rh.run_once(t.id)
    await rh.run_once(t.id)
    assert alpha.requests[1].resume_session_id is None


async def test_worker_request_carries_permission_cwd_timeout_and_brief(rh):
    p = await rh.project()
    t = await _task(rh, p, acceptance_criteria=["the layer returns rows"])
    assert await rh.run_once(t.id) == S.COMPLETED
    req = rh.mock.requests[0]
    task = await rh.tasks.get(t.id)
    assert req.permission == "workspace_write" and req.cwd == task.worktree_path
    assert req.timeout_seconds == 60  # policies limits.max_active_runtime_seconds (FAST_LIMITS)
    assert "Work only inside this directory" in req.prompt and req.cwd in req.prompt
    assert "implement the repository layer" in req.prompt and "the layer returns rows" in req.prompt
    assert "## Objective" in req.prompt  # handoff included


async def test_plan_phase_is_read_only(rh):
    p = await rh.project()
    t = await rh.task(p.id, preferred_provider=None, verify_commands=OK, autostart=True, title="implement the repository layer")
    assert await rh.run_once(t.id) == S.COMPLETED  # no acceptance criteria -> a planning run first
    assert [r.permission for r in rh.mock.requests][:2] == ["read_only", "workspace_write"]


# ---- permission escalation (known gap) ------------------------------------------------------------------------


async def test_execute_phase_escalation_says_nothing_is_granted(rh):
    p = await rh.project()
    rh.mock.queue(MockScript.failure(ErrorCategory.PERMISSION_DENIED, "needs network"), MockScript.success())
    t = await _task(rh, p)
    assert await rh.run_once(t.id) == S.NEEDS_APPROVAL
    [a] = await rh.approvals.list("pending", t.id)
    assert a.kind == "permission_escalation" and "NO additional permission" in a.summary
    assert a.details["grants"] is None and a.details["permission"] == "workspace_write" and a.details["phase"] == "execute"
    await rh.approvals.resolve(a.id, True)
    assert (await rh.tasks.get(t.id)).granted_permission is None
    assert await rh.run_once(t.id) == S.COMPLETED
    assert rh.mock.requests[1].permission == "workspace_write"


async def test_read_only_phase_escalation_grants_workspace_write_for_that_phase(rh):
    p = await rh.project()
    rh.mock.queue(MockScript.failure(ErrorCategory.PERMISSION_DENIED, "plan wanted to write"), MockScript.success())
    t = await rh.task(p.id, preferred_provider=None, verify_commands=OK, autostart=True, title="implement the repository layer")
    assert await rh.run_once(t.id) == S.NEEDS_APPROVAL  # the plan run was denied
    [a] = await rh.approvals.list("pending", t.id)
    assert a.details["grants"] == "workspace_write" and "never adds dangerous-skip flags" in a.summary
    assert "confined" not in a.summary  # unknown (mock) provider: no confinement claim
    assert rh.mock.requests[0].permission == "read_only"
    await rh.approvals.resolve(a.id, True)
    task = await rh.tasks.get(t.id)
    assert task.granted_permission == {"permission": "workspace_write", "phase": "plan", "approval_id": a.id}
    assert await rh.run_once(t.id) == S.COMPLETED
    assert rh.mock.requests[1].permission == "workspace_write"  # the re-run plan phase got the grant
    assert rh.mock.requests[2].permission == "workspace_write"  # execute phase as before
    # an explicit retry clears routing/permission state
    await rh.tasks.set_fields(t.id, {"granted_permission": None})
