"""Full pipeline with MockProvider in a real temporary git repository."""

import os
from pathlib import Path

from orchestrator.db import records
from orchestrator.db import runs as runs_repo
from orchestrator.domain.states import AgentActivity, TaskState
from orchestrator.providers.mock import MockScript
from orchestrator.testing import git, py_check

S = TaskState

CHECK_HELLO = py_check("import pathlib,sys; sys.exit(0 if pathlib.Path('hello.txt').read_text()=='hi' else 1)")


async def _runs(h, task_id):
    return await h.ctx.db.read(lambda tx: runs_repo.for_task(tx, task_id))


async def test_success_creates_worktree_and_completes(h):
    p = await h.project()
    h.mock.queue(MockScript.success("wrote hello", files={"hello.txt": "hi"}))
    t = await h.task(p.id, acceptance_criteria=["hello.txt says hi"], verify_commands=[CHECK_HELLO, {"file_exists": "hello.txt"}], autostart=True)

    assert await h.run_once(t.id) == S.COMPLETED
    t = await h.tasks.get(t.id)
    wt = Path(t.worktree_path)
    assert wt.is_dir() and wt.parent == h.ctx.paths.worktrees_dir
    assert t.branch == f"orch/{t.id}"
    assert (wt / "hello.txt").read_text() == "hi"
    assert not (Path(p.path) / "hello.txt").exists()  # project checkout untouched
    assert git(wt, "rev-parse", "--abbrev-ref", "HEAD").strip() == t.branch
    assert t.head_commit and t.head_commit != t.base_commit
    assert "hello.txt" in git(wt, "show", "--name-only", "--format=", "HEAD")

    runs = await _runs(h, t.id)
    assert [(r.phase.value, r.status.value, r.idempotency_key) for r in runs] == [("execute", "succeeded", f"{t.id}:execute:1")]
    assert runs[0].usage.get("input_tokens") == 10 and runs[0].session_id

    v = await h.ctx.db.read(lambda tx: records.latest_verification(tx, t.id))
    assert v.status == "passed" and len(v.checks) == 2
    cps = await h.executor.checkpoints.list(t.id)
    assert cps and cps[0].git_ref == t.head_commit and cps[0].summary["objective"].startswith("demo task")

    types = [e.type for e in await h.ctx.bus.replay(0, task_id=t.id, limit=500)]
    for needed in ("task.created", "router.decision", "run.started", "run.output", "run.usage", "run.finished",
                   "checkpoint.created", "verification.result"):
        assert needed in types, needed
    states = [e.payload["to"] for e in await h.ctx.bus.replay(0, task_id=t.id, types=["task.state_changed"])]
    assert states == ["QUEUED", "RUNNING", "TESTING", "COMPLETED"]
    seat = [a for a in await h.agents.list() if a.id == runs[0].agent_id][0]
    assert seat.activity == AgentActivity.IDLE and seat.current_task_id is None
    acts = [e.payload["to"] for e in await h.ctx.bus.replay(0, types=["agent.activity_changed"])]
    assert acts == ["coding", "idle", "testing", "idle", "celebrating", "idle"]


async def test_verification_failure_repair_loop_is_bounded(h):
    await h.set_limits(max_repair_cycles=2)
    p = await h.project()
    # Model always claims success but never writes the right content.
    h.mock._default = MockScript.success("all done, tests pass!", files={"hello.txt": "nope"})
    t = await h.task(p.id, acceptance_criteria=["hello says hi"], verify_commands=[CHECK_HELLO], autostart=True)

    assert await h.run_once(t.id) == S.FAILED
    t = await h.tasks.get(t.id)
    assert t.repair_cycles == 2 and "verification failed" in t.state_reason
    runs = await _runs(h, t.id)
    assert [r.idempotency_key for r in runs] == [f"{t.id}:execute:1", f"{t.id}:repair:1", f"{t.id}:repair:2"]
    # verification failures are fed back into the repair prompts via the handoff
    repair_prompts = [r.prompt for r in h.mock.requests[1:]]
    assert all("REPAIR" in pr and "Test status" in pr and "failed" in pr for pr in repair_prompts)
    vs = await h.ctx.db.read(lambda tx: records.verifications_for_task(tx, t.id))
    assert [v.status for v in vs] == ["failed", "failed", "failed"]
    states = [e.payload["to"] for e in await h.ctx.bus.replay(0, task_id=t.id, types=["task.state_changed"])]
    assert "COMPLETED" not in states
    assert states.count("RUNNING") == 3


async def test_model_claims_success_without_verification_pass_is_not_completed(h):
    await h.set_limits(max_repair_cycles=0)
    p = await h.project()
    h.mock.queue(MockScript.success("I verified everything, it works", structured_output={"status": "done"}))
    t = await h.task(p.id, acceptance_criteria=["x"], verify_commands=[py_check("import sys; sys.exit(3)")], autostart=True)
    assert await h.run_once(t.id) == S.FAILED
    v = await h.ctx.db.read(lambda tx: records.latest_verification(tx, t.id))
    assert v.status == "failed" and v.checks[0].exit_code == 3


async def test_repair_succeeds_on_second_attempt(h):
    p = await h.project()
    h.mock.queue(MockScript.success("first", files={"hello.txt": "bad"}), MockScript.success("fixed", files={"hello.txt": "hi"}))
    t = await h.task(p.id, acceptance_criteria=["x"], verify_commands=[CHECK_HELLO], autostart=True)
    assert await h.run_once(t.id) == S.COMPLETED
    runs = await _runs(h, t.id)
    assert [r.phase.value for r in runs] == ["execute", "repair"]


async def test_no_verification_needs_approval_then_accept(h):
    p = await h.project()
    t = await h.task(p.id, acceptance_criteria=["something subjective"], autostart=True)
    assert await h.run_once(t.id) == S.NEEDS_APPROVAL
    pending = await h.approvals.list("pending", t.id)
    assert [a.kind for a in pending] == ["unverifiable_completion"]
    await h.approvals.resolve(pending[0].id, True, note="looks fine")
    t = await h.tasks.get(t.id)
    assert t.state == S.COMPLETED
    v = await h.ctx.db.read(lambda tx: records.latest_verification(tx, t.id))
    assert v.status == "unverifiable_accepted"


async def test_planning_phase_when_no_criteria(h):
    p = await h.project()
    h.mock.queue(
        MockScript.success("plan", structured_output={"steps": ["write hello"], "acceptance_criteria": ["hello says hi"]}),
        MockScript.success("did it", files={"hello.txt": "hi"}),
    )
    t = await h.task(p.id, verify_commands=[CHECK_HELLO], autostart=True)
    assert await h.run_once(t.id) == S.COMPLETED
    t = await h.tasks.get(t.id)
    assert t.acceptance_criteria == ["hello says hi"] and t.pending_steps == ["write hello"]
    runs = await _runs(h, t.id)
    assert [r.phase.value for r in runs] == ["plan", "execute"]
    assert h.mock.requests[0].permission == "read_only"
    assert h.mock.requests[1].permission == "workspace_write"
    states = [e.payload["to"] for e in await h.ctx.bus.replay(0, task_id=t.id, types=["task.state_changed"])]
    assert states == ["QUEUED", "PLANNING", "RUNNING", "TESTING", "COMPLETED"]


async def test_review_required_reject_then_approve(h):
    await h.ctx.db.write(lambda tx: records.setting_put(tx, "review", {"required": True}))
    p = await h.project()
    h.mock.queue(
        MockScript.success("v1", files={"hello.txt": "hi"}),
        MockScript.success("review", structured_output={"verdict": "reject", "issues": ["add a newline"]}),
        MockScript.success("v2", files={"hello.txt": "hi"}),
        MockScript.success("review", structured_output={"verdict": "approve"}),
    )
    t = await h.task(p.id, acceptance_criteria=["x"], verify_commands=[CHECK_HELLO], autostart=True)
    assert await h.run_once(t.id) == S.COMPLETED
    runs = await _runs(h, t.id)
    assert [r.phase.value for r in runs] == ["execute", "review", "repair", "review"]
    assert "add a newline" in h.mock.requests[2].prompt


async def test_non_git_project_blocks_until_git_init_and_explicit_retry(h):
    p = await h.project("plain", git_repo=False)
    assert p.isolation == "in_place" and not p.is_git
    pending = await h.approvals.list("pending")
    assert [a.kind for a in pending] == ["git_init_in_project"]
    h.mock.queue(MockScript.success("ok", files={"out.txt": "x"}))
    t = await h.task(p.id, acceptance_criteria=["x"], verify_commands=[{"file_exists": "out.txt"}], autostart=True)
    assert await h.run_once(t.id) == S.BLOCKED
    assert not h.mock.requests
    assert not (Path(p.path) / "out.txt").exists()
    # approving git init converts the project to worktree isolation
    await h.approvals.resolve(pending[0].id, True)
    p2 = await h.projects.get(p.id)
    assert p2.is_git and p2.isolation == "worktree"
    assert os.path.isdir(os.path.join(p.path, ".git"))
    await h.scheduler.retry_task(t.id)
    assert await h.run_once(t.id) == S.COMPLETED
    assert not (Path(p.path) / "out.txt").exists()
    current = await h.tasks.get(t.id)
    assert (Path(current.worktree_path) / "out.txt").read_text() == "x"
