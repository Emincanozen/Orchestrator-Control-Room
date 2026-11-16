"""Advisor: isolation from the leader's transcript, provider independence, budgets, policies, decision records."""

from __future__ import annotations

import pytest

from orchestrator.domain.states import TaskState as S
from orchestrator.errors import Conflict, ValidationFailed
from orchestrator.leader.advisor import AdvisorRefused
from orchestrator.leader.service import LeadershipUpdate
from orchestrator.testing import FAIL_CHECK, PASS_CHECK, make_git_repo, py_check

from .conftest import GOOD_PLAN

SECRET = "LEADER-PRIVATE-REASONING-7f3a"


async def consult(h, **kw):
    kw.setdefault("trigger", "user")
    kw.setdefault("problem", "Should we cache this?")
    return await h.stack.advisor.consult(**kw)


async def test_advisor_prompt_contains_only_problem_requirements_evidence_options_constraints(lh):
    p = await lh.project()
    t = await lh.mk_task(
        p.id, title="Speed up search", description="make search fast", acceptance_criteria=["p95 < 100ms"],
        plan={"steps": [SECRET], "notes": SECRET}, pending_steps=[SECRET],
    )
    await lh.tasks.set_fields(t.id, {"next_action": SECRET})
    lh.brain.push("plan", {**GOOD_PLAN, "steps": [SECRET]})
    res = await consult(lh, task_id=t.id, options=["add an LRU cache", "use an index"], constraints=["no new dependencies"])
    prompt = lh.brain.prompts["advisor"][0]
    assert SECRET not in prompt  # nothing from the leader's plan / next action / outputs
    for needed in ("Should we cache this?", "Speed up search", "p95 < 100ms", "1. add an LRU cache", "2. use an index", "no new dependencies", "## Evidence"):
        assert needed in prompt
    assert res["result"]["recommended_option"] and res["trigger"] == "user"
    # the run is phase advise on the advisor seat, read-only
    run = await lh.ctx.db.read(lambda tx: tx.one("SELECT * FROM runs WHERE id = ?", (res["run_id"],)))
    assert run["phase"] == "advise" and run["agent_id"] == "agt_advisor" and run["task_id"] == t.id
    req = lh.providers[res["provider"]].requests[-1]
    assert req.permission == "read_only"


async def test_advisor_prefers_a_different_provider_than_the_leader(lh):
    res = await consult(lh)
    assert res["provider"] == "beta" and res["independence"] == "different_provider"
    ev = lh.events("advisor.consulted")[0]["payload"]
    assert ev["independence"] == "different_provider" and ev["provider"] == "beta"


async def test_same_provider_is_reported_honestly_when_nothing_else_exists(make_lh):
    h = await make_lh(spec={"alpha": {"models": [("alpha-model", "frontier")]}})
    res = await consult(h)
    assert res["provider"] == "alpha" and res["independence"] == "same_provider"
    [d] = await h.stack.decisions.list()
    assert d.meta["independence"] == "same_provider"


async def test_configured_advisor_is_pinned(lh):
    await lh.stack.leadership.update(LeadershipUpdate(advisor={"provider": "alpha", "model": "alpha-model"}))
    res = await consult(lh)
    assert res["provider"] == "alpha" and res["independence"] == "same_provider"


async def test_per_task_budget_gives_a_clear_refusal(lh):
    p = await lh.project()
    t = await lh.mk_task(p.id, title="t")
    await lh.stack.leadership.update(LeadershipUpdate(max_consultations_per_task=1))
    await consult(lh, task_id=t.id)
    with pytest.raises(AdvisorRefused) as e:
        await consult(lh, task_id=t.id)
    assert e.value.details["reason"] == "budget_per_task" and (e.value.details["used"], e.value.details["limit"]) == (1, 1)
    assert e.value.status == 429 and e.value.code == "advisor_budget_exhausted"
    assert len(lh.brain.prompts["advisor"]) == 1  # refused before any call


async def test_per_day_budget_gives_a_clear_refusal(lh):
    await lh.stack.leadership.update(LeadershipUpdate(max_consultations_per_day=2))
    await consult(lh)
    await consult(lh)
    with pytest.raises(AdvisorRefused) as e:
        await consult(lh)
    assert e.value.details["reason"] == "budget_per_day" and e.value.details["limit"] == 2
    assert (await lh.stack.leadership.budgets())["consultations_today"] == {"used": 2, "limit": 2}


async def test_policy_never_and_auto_only_triggers(lh):
    await lh.stack.leadership.update(LeadershipUpdate(advisor_policy="never"))
    with pytest.raises(AdvisorRefused) as e:
        await consult(lh)
    assert e.value.details["reason"] == "disabled"
    await lh.stack.leadership.update(LeadershipUpdate(advisor_policy="on_request"))
    for trig in ("failed_repairs", "high_risk"):
        with pytest.raises(AdvisorRefused) as e:
            await consult(lh, trigger=trig)
        assert e.value.details["reason"] == "trigger_not_enabled"
    assert (await consult(lh, trigger="leader_escalation"))["trigger"] == "leader_escalation"
    assert "advisor" in lh.brain.prompts and len(lh.brain.prompts["advisor"]) == 1


async def test_invalid_advisor_output_is_refused_after_one_repair(lh):
    lh.brain.push("advisor", "garbage", {"assessment": ""})
    with pytest.raises(AdvisorRefused) as e:
        await consult(lh)
    assert e.value.details["reason"] == "invalid_output" and len(lh.brain.prompts["advisor"]) == 2


async def test_unknown_task_is_not_found(lh):
    from orchestrator.errors import NotFound

    with pytest.raises(NotFound):
        await consult(lh, task_id="tsk_missing")


async def test_decision_records_evidence_status(lh, tmp_path):
    # no task, no evidence: stored, but flagged
    r = await consult(lh)
    d = await lh.stack.decisions.get(r["decision_id"])
    assert d.kind == "advisor_consult" and d.evidence_status == "none" and "no_objective_evidence" in d.meta["flags"]
    assert d.advisor_run_id == r["run_id"] and d.decision == r["result"]["recommended_option"]
    # a task with a passing verification and a worktree diff gives objective evidence
    p = await lh.project()
    t = await lh.mk_task(p.id, title="t", acceptance_criteria=["c"], plan={"x": 1}, verify_commands=PASS_CHECK)
    await lh.tasks.transition(t.id, S.QUEUED, reason="s")
    assert await lh.run_once(t.id) == S.COMPLETED
    r2 = await consult(lh, task_id=t.id)
    d2 = await lh.stack.decisions.get(r2["decision_id"])
    types = {e["type"] for e in d2.evidence}
    assert d2.evidence_status == "verified" and {"verification", "diff", "commit", "run"} <= types
    assert "hello.txt" in lh.brain.prompts["advisor"][-1]  # the bounded diff is part of the evidence
    assert [x.id for x in await lh.stack.decisions.list(task_id=t.id)] == [d2.id]
    assert len(await lh.stack.decisions.list(project_id=p.id)) == 1 and len(await lh.stack.decisions.list()) == 2
    assert len(lh.events("decision.recorded")) == 2


async def test_auto_policy_consults_before_executing_a_high_risk_task(lh):
    await lh.stack.leadership.update(LeadershipUpdate(advisor_policy="auto"))
    p = await lh.project()
    t = await lh.mk_task(p.id, title="Rotate authentication token handling", acceptance_criteria=["c"], plan={"x": 1},
                         verify_commands=PASS_CHECK)
    await lh.tasks.transition(t.id, S.QUEUED, reason="s")
    assert await lh.run_once(t.id) == S.COMPLETED
    [d] = await lh.stack.decisions.list(task_id=t.id)
    assert d.meta["trigger"] == "high_risk"
    assert any("Advisor (different_provider) recommends" in x for x in lh.brain.prompts["execute"])  # guidance reached the worker
    # low-risk tasks are not consulted
    t2 = await lh.mk_task(p.id, title="rename variable", acceptance_criteria=["c"], plan={"x": 1}, verify_commands=PASS_CHECK)
    await lh.tasks.transition(t2.id, S.QUEUED, reason="s")
    await lh.run_once(t2.id)
    assert await lh.stack.decisions.list(task_id=t2.id) == []


async def test_auto_policy_consults_after_n_failed_repairs(lh):
    await lh.stack.leadership.update(LeadershipUpdate(advisor_policy="auto", advisor_after_failed_repairs=1, max_consultations_per_task=1))
    await lh.set_limits(max_repair_cycles=3)
    p = await lh.project()
    t = await lh.mk_task(p.id, title="flaky", acceptance_criteria=["c"], plan={"x": 1}, verify_commands=FAIL_CHECK)
    await lh.tasks.transition(t.id, S.QUEUED, reason="s")
    assert await lh.run_once(t.id) == S.FAILED
    ds = await lh.stack.decisions.list(task_id=t.id)
    assert [d.meta["trigger"] for d in ds] == ["failed_repairs"]  # budget (1) stops further consults, loop stays bounded
    assert any("Advisor (" in x for x in lh.brain.prompts["execute"][1:])
    assert [e for e in lh.events("system.notice") if "refused" in e["payload"].get("message", "")]  # the refusal is announced


async def test_export_appends_adr_entry_only_on_request_inside_the_project(lh):
    p = await lh.project()
    t = await lh.mk_task(p.id, title="t")
    r = await consult(lh, task_id=t.id)
    from pathlib import Path

    docs = Path(p.path) / "docs" / "DECISIONS.md"
    assert not docs.exists()  # nothing is written by recording
    out = await lh.stack.decisions.export(r["decision_id"])
    assert Path(out["path"]) == docs.resolve() and docs.is_file()
    text = docs.read_text(encoding="utf-8")
    assert f"<!-- decision:{r['decision_id']} -->" in text and "Advisor consult" in text and "flagged" in text
    with pytest.raises(Conflict) as e:
        await lh.stack.decisions.export(r["decision_id"])
    assert e.value.code == "already_exported"
    # a decision without any project cannot be exported
    r2 = await consult(lh)
    with pytest.raises(ValidationFailed):
        await lh.stack.decisions.export(r2["decision_id"])
