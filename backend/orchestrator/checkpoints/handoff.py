"""Build a concise Handoff (short-term task context) from DB state + git."""

from __future__ import annotations

import os
from typing import Optional

from orchestrator.db import records
from orchestrator.db import runs as runs_repo
from orchestrator.db.database import Tx
from orchestrator.domain import events as ev
from orchestrator.domain.models import GitSummary, Handoff, Task, TestStatus, Verification
from orchestrator.gitops import git
from orchestrator.security.redact import redact
from orchestrator.services.context import AppContext
from orchestrator.util import loads, truncate

MAX_ITEMS = 15
FAILURE_OUTPUT_LIMIT = 3000


def _describe_spec(spec) -> str:
    if isinstance(spec, list):
        return " ".join(spec)
    if isinstance(spec, str):
        return spec
    data = spec.model_dump(exclude_none=True) if hasattr(spec, "model_dump") else dict(spec)
    if data.get("file_exists"):
        return f"file exists: {data['file_exists']}"
    cmd = data.get("cmd")
    return " ".join(cmd) if isinstance(cmd, list) else str(cmd)


def _db_part(tx: Tx, task_id: str) -> dict:
    runs = runs_repo.for_task(tx, task_id)
    finished = tx.all(
        "SELECT run_id, payload FROM events WHERE task_id = ? AND type = ? ORDER BY seq DESC LIMIT 10",
        (task_id, ev.RUN_FINISHED),
    )
    decisions = tx.all(
        "SELECT title, decision FROM decisions WHERE task_id = ? ORDER BY created_at DESC LIMIT ?", (task_id, MAX_ITEMS)
    )
    return {
        "runs": runs,
        "finished": [(r["run_id"], loads(r["payload"], {})) for r in finished],
        "decisions": [f"{d['title']}: {d['decision']}" for d in decisions],
        "verification": records.latest_verification(tx, task_id),
    }


def _test_status(v: Optional[Verification]) -> TestStatus:
    if v is None:
        return TestStatus()
    failed = [c for c in v.checks if not c.passed]
    output = "\n".join(f"[{c.name}] {c.error or ''}\n{c.output}".strip() for c in failed)
    return TestStatus(
        status=v.status,
        failed_checks=[c.name for c in failed][:MAX_ITEMS],
        failure_output=truncate(redact(output), FAILURE_OUTPUT_LIMIT),
    )


def git_summary(task: Task) -> GitSummary:
    wt = task.worktree_path
    if not wt or not os.path.isdir(wt):
        return GitSummary(branch=task.branch, base_commit=task.base_commit, head_commit=task.head_commit)
    try:
        if not git.is_repo(wt):
            return GitSummary()
        head = git.rev_parse(wt)
        base = task.base_commit
        commits = git.log_oneline(wt, f"{base}..HEAD", max_count=MAX_ITEMS) if base and head else []
        stat = git.diff(wt, base, stat=True, max_chars=4000) if base else ""
        changed = git.changed_files(wt, base)
        uncommitted = git.has_changes(wt)
    except git.GitError:
        return GitSummary(branch=task.branch, base_commit=task.base_commit, head_commit=task.head_commit)
    return GitSummary(
        branch=task.branch or git.current_branch(wt),
        base_commit=base,
        head_commit=head,
        commits=commits,
        diff_stat=stat.strip(),
        changed_files=changed[:100],
        uncommitted_changes=uncommitted,
    )


async def build_handoff(ctx: AppContext, task: Task, *, include_git: bool = True) -> Handoff:
    part = await ctx.db.read(lambda tx: _db_part(tx, task.id))
    g = await git.agit(git_summary, task) if include_git else GitSummary()
    completed: list[str] = [str(s) for s in task.completed_steps][:MAX_ITEMS]
    known: list[str] = []
    for run in reversed(part["runs"]):
        if len(completed) + len(known) >= 2 * MAX_ITEMS:
            break
        if run.status.value == "succeeded" and run.phase.value in ("execute", "repair"):
            completed.append(f"{run.phase.value} run {run.id} by {run.provider}/{run.model or '?'} succeeded")
        elif run.status.value in ("failed", "interrupted", "cancelled"):
            known.append(
                f"{run.phase.value} run {run.id} {run.status.value}"
                + (f" ({run.error_category})" if run.error_category else "")
                + (f": {truncate(redact(run.error_message), 300, keep='head')}" if run.error_message else "")
            )
    for _run_id, payload in part["finished"][:3]:
        summary = payload.get("summary")
        if summary:
            completed.append(f"agent summary: {truncate(summary, 400, keep='head')}")
    ts = _test_status(part["verification"])
    if task.next_action:
        next_action = task.next_action
    elif ts.status in ("failed", "error"):
        next_action = "Fix the failing verification checks listed under Test status, then stop."
    else:
        next_action = "Implement the objective so that every requirement and verification check passes."
    requirements = list(task.acceptance_criteria) + [f"verification must pass: {_describe_spec(s)}" for s in task.verify_commands]
    relevant = list(dict.fromkeys(list(task.relevant_files) + g.changed_files))[: 3 * MAX_ITEMS]
    return Handoff(
        task_id=task.id,
        objective=(task.title + ("\n\n" + task.description if task.description else "")).strip(),
        requirements=requirements[: 2 * MAX_ITEMS],
        completed_work=completed[: 2 * MAX_ITEMS],
        remaining_work=[str(s) for s in task.pending_steps][:MAX_ITEMS],
        decisions=part["decisions"],
        relevant_files=relevant,
        git=g,
        test_status=ts,
        known_issues=known[:MAX_ITEMS],
        next_action=next_action,
    )
