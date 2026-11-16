"""Objective decomposition: strict validation, tasks + dependencies, derived status, recorded leader run."""

from __future__ import annotations

import copy

from orchestrator.domain.states import TaskState as S

from .conftest import GOOD_DECOMPOSITION


def d(**kw):
    out = copy.deepcopy(GOOD_DECOMPOSITION)
    out.update(kw)
    return out


def task(key, deps=(), **kw):
    t = {"key": key, "title": f"task {key}", "description": "", "task_type": "coding", "role": "backend", "priority": 50,
         "depends_on": list(deps), "acceptance_criteria": ["ok"], "verify_commands": []}
    t.update(kw)
    return t


async def decompose(h, p, text="Add hello.txt"):
    return await h.stack.objectives.create(p.id, text, wait=True)


async def test_decomposition_creates_tasks_in_created_state_with_dependencies(lh):
    p = await lh.project()
    obj = await decompose(lh, p)
    assert obj["status"] == "planned" and obj["deliverables"] == ["hello.txt"]
    by_key = {t["key"]: t for t in obj["tasks"]}
    assert set(by_key) == {"a", "b"} and all(t["state"] == "CREATED" for t in obj["tasks"])
    assert by_key["b"]["depends_on"] == [by_key["a"]["id"]] and by_key["a"]["depends_on"] == []
    stored = await lh.tasks.get(by_key["a"]["id"])
    assert stored.objective_id == obj["id"] and stored.acceptance_criteria == ["file exists"] and stored.role == "backend"
    assert (await lh.tasks.get(by_key["b"]["id"])).verify_commands == ["python -m pytest -q"]
    # recorded as a leader run on the leader seat, with events, no task attached
    row = await lh.ctx.db.read(lambda tx: tx.one("SELECT * FROM runs WHERE phase = 'plan'"))
    assert row["task_id"] is None and row["agent_id"] == "agt_leader" and row["provider"] == "alpha" and row["status"] == "succeeded"
    started = [e for e in lh.events("run.started") if e["run_id"] == row["id"]]
    assert started and started[0]["payload"]["purpose"] == "decompose"
    assert obj["run_ids"] == [row["id"]]
    assert "Add hello.txt" in lh.brain.prompts["decompose"][0]


async def test_tasks_do_not_start_until_started_and_autostart_queues_them(lh):
    p = await lh.project()
    obj = await decompose(lh, p)
    await lh.scheduler.tick()
    assert [(await lh.tasks.get(t["id"])).state for t in obj["tasks"]] == [S.CREATED, S.CREATED]
    auto = await lh.stack.objectives.create(p.id, "again", autostart=True, wait=True)
    assert all(t["state"] == "QUEUED" for t in auto["tasks"]) and auto["status"] == "running"


async def test_invalid_decompositions_are_rejected_after_one_repair(lh):
    p = await lh.project()
    cases = {
        "cycle": d(tasks=[task("a", ["b"]), task("b", ["a"])]),
        "duplicate": d(tasks=[task("a"), task("a")]),
        "unknown dependency": d(tasks=[task("a", ["zzz"])]),
        "unknown type": d(tasks=[task("a", task_type="wizardry")]),
        "unknown role": d(tasks=[task("a", role="ceo")]),
        "self dependency": d(tasks=[task("a", ["a"])]),
        "oversize": d(tasks=[task(f"t{i}") for i in range(31)]),
        "empty": d(tasks=[]),
    }
    for name, bad in cases.items():
        lh.brain.push("decompose", bad, bad)
        obj = await decompose(lh, p, name)
        assert obj["status"] == "failed", name
        assert obj["tasks"] == [] and obj["error"] and "decomposition is invalid after 2 attempts" in obj["error"], name
    assert await lh.ctx.db.read(lambda tx: tx.scalar("SELECT COUNT(*) FROM tasks")) == 0


async def test_a_repair_attempt_can_fix_a_cycle(lh):
    p = await lh.project()
    lh.brain.push("decompose", d(tasks=[task("a", ["b"]), task("b", ["a"])]), d(tasks=[task("a"), task("b", ["a"])]))
    obj = await decompose(lh, p)
    assert obj["status"] == "planned" and len(obj["tasks"]) == 2
    assert "dependency cycle" in lh.brain.prompts["decompose"][1]


async def test_unsafe_verify_commands_are_not_attached(lh):
    p = await lh.project()
    lh.brain.push("decompose", d(tasks=[task("a", verify_commands=["pytest -q", "rm -rf /"])]))
    obj = await decompose(lh, p)
    t = await lh.tasks.get(obj["tasks"][0]["id"])
    assert t.verify_commands == [] and t.plan["unsafe_verify_commands"][0]["command"] == "rm -rf /"
    assert obj["unsafe_verify_commands"]["a"]


async def test_status_is_derived_from_real_task_states(lh):
    p = await lh.project()
    obj = await decompose(lh, p)
    ids = {t["key"]: t["id"] for t in obj["tasks"]}
    svc = lh.stack.objectives
    assert (await svc.get(obj["id"]))["status"] == "planned"
    await lh.tasks.transition(ids["a"], S.QUEUED, reason="s")
    assert (await svc.get(obj["id"]))["status"] == "running"
    await lh.tasks.transition(ids["a"], S.CANCELLED, reason="c")
    assert (await svc.get(obj["id"]))["status"] == "failed"
    # all completed -> completed (verification required: give both a passing record through the real executor path)
    obj2 = await decompose(lh, p, "second")
    for t in obj2["tasks"]:
        await lh.tasks.set_fields(t["id"], {"verify_commands": [["python", "-c", "print(1)"]]})
        await lh.tasks.transition(t["id"], S.QUEUED, reason="s")
    for t in sorted(obj2["tasks"], key=lambda x: x["key"]):
        assert await lh.run_once(t["id"]) == S.COMPLETED
    done = await svc.get(obj2["id"])
    assert done["status"] == "completed" and done["task_counts"] == {"COMPLETED": 2}
    stored = await lh.ctx.db.read(lambda tx: tx.scalar("SELECT status FROM objectives WHERE id = ?", (obj2["id"],)))
    assert stored == "completed"  # persisted, not only computed


async def test_failed_call_marks_objective_failed(lh):
    from orchestrator.providers.mock import MockScript
    from orchestrator.providers.base import ErrorCategory

    p = await lh.project()
    lh.brain.push("decompose", MockScript.failure(ErrorCategory.CRASHED, "boom"))
    obj = await decompose(lh, p)
    assert obj["status"] == "failed" and "boom" in obj["error"]


async def test_background_create_returns_planning_then_planned(lh):
    import asyncio

    p = await lh.project()
    obj = await lh.stack.objectives.create(p.id, "bg")
    assert obj["status"] in ("planning", "planned")
    for _ in range(100):
        cur = await lh.stack.objectives.get(obj["id"])
        if cur["status"] != "planning":
            break
        await asyncio.sleep(0.05)
    assert cur["status"] == "planned"
