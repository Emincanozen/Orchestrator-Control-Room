"""Commands for projects, tasks, running/controlling tasks, logs and approvals."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Optional

import typer

from orchestrator.cli import follow as follow_mod
from orchestrator.cli.client import ApiError
from orchestrator.cli.common import JsonOpt, Session, UrlOpt, fail, session
from orchestrator.cli.output import Out, short

projects_app = typer.Typer(help="Registered projects (repositories the orchestrator may work in).", no_args_is_help=True)
tasks_app = typer.Typer(help="Tasks: list, inspect and create.", no_args_is_help=True)
approvals_app = typer.Typer(help="Approval requests that need a human decision.", no_args_is_help=True)

# ---- shared task options ---------------------------------------------------------------------------
ProjectOpt = Annotated[Optional[str], typer.Option("--project", help="Project id.")]
TitleOpt = Annotated[Optional[str], typer.Option("--title", help="Task title.")]
DescOpt = Annotated[Optional[str], typer.Option("--description", help="Task description / instructions.")]
DescFileOpt = Annotated[
    Optional[Path], typer.Option("--description-file", help="Read the description from this UTF-8 text file.")
]
TypeOpt = Annotated[Optional[str], typer.Option("--type", help="Task type (default: coding).")]
RoleOpt = Annotated[Optional[str], typer.Option("--role", help="Agent role override (e.g. developer, reviewer).")]
PriorityOpt = Annotated[Optional[int], typer.Option("--priority", min=0, max=100, help="0-100 (default 50).")]
DependsOpt = Annotated[Optional[list[str]], typer.Option("--depends-on", help="Task id this one waits for (repeatable).")]
CriteriaOpt = Annotated[Optional[list[str]], typer.Option("--criteria", help="Acceptance criterion (repeatable).")]
VerifyOpt = Annotated[
    Optional[list[str]], typer.Option("--verify-cmd", help="Verification command, run in the workspace (repeatable).")
]
ProviderOpt = Annotated[Optional[str], typer.Option("--provider", help="Preferred provider (claude, codex, antigravity).")]
ModelOpt = Annotated[Optional[str], typer.Option("--model", help="Preferred model id.")]


def build_task_body(
    project: Optional[str], title: Optional[str], description: Optional[str], description_file: Optional[Path],
    task_type: Optional[str], role: Optional[str], priority: Optional[int], depends_on: Optional[list[str]],
    criteria: Optional[list[str]], verify_cmd: Optional[list[str]], provider: Optional[str], model: Optional[str],
) -> dict[str, Any]:
    if not project:
        raise fail("--project is required")
    if not title:
        raise fail("--title is required")
    if description is not None and description_file is not None:
        raise fail("use either --description or --description-file, not both")
    if description_file is not None:
        try:
            description = description_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise fail(f"cannot read {description_file}: {exc.__class__.__name__}") from None
    body: dict[str, Any] = {"project_id": project, "title": title}
    for key, value in (
        ("description", description), ("task_type", task_type), ("role", role), ("priority", priority),
        ("depends_on", depends_on), ("acceptance_criteria", criteria), ("verify_commands", verify_cmd),
        ("preferred_provider", provider), ("preferred_model", model),
    ):
        if value is not None and value != []:
            body[key] = value
    return body


# ---- renderers -------------------------------------------------------------------------------------
def render_projects(out: Out, data: list[dict[str, Any]]) -> None:
    out.table(
        ["id", "name", "path", "git", "verify"],
        [[p["id"], p["name"], p["path"], "yes" if p.get("is_git") else "no", len(p.get("verify_commands") or [])] for p in data],
        keep=["id"],
    )


def render_project(out: Out, p: dict[str, Any]) -> None:
    out.kv([
        ("id", p["id"]), ("name", p["name"]), ("path", p["path"]), ("git repository", "yes" if p.get("is_git") else "no"),
        ("default branch", p.get("default_branch") or "unknown"), ("isolation", p.get("isolation")),
        ("created", p.get("created_at")),
    ])
    cmds = p.get("verify_commands") or []
    out.line("verify commands: " + ("(none)" if not cmds else ""))
    for c in cmds:
        out.line(f"  - {c if isinstance(c, str) else c}")


def render_tasks(out: Out, data: list[dict[str, Any]]) -> None:
    out.table(
        ["id", "state", "pri", "type", "provider/model", "title"],
        [
            [t["id"], t["state"], t.get("priority"), t.get("task_type"),
             "/".join(x for x in (t.get("preferred_provider"), t.get("preferred_model")) if x) or "-", short(t["title"], 70)]
            for t in data
        ],
        keep=["id", "state"],
    )


def render_task(out: Out, t: dict[str, Any]) -> None:
    out.kv([
        ("id", t["id"]), ("title", t["title"]), ("project", t["project_id"]), ("state", t["state"]),
        ("state reason", t.get("state_reason") or "-"), ("type", t.get("task_type")), ("role", t.get("role") or "-"),
        ("priority", t.get("priority")),
        ("preferred", "/".join(x for x in (t.get("preferred_provider"), t.get("preferred_model")) if x) or "-"),
        ("depends on", ", ".join(t.get("depends_on") or []) or "-"),
        ("attempts", t.get("attempt_count")), ("retries", t.get("retry_count")), ("branch", t.get("branch") or "-"),
        ("workspace", t.get("worktree_path") or "-"), ("created", t.get("created_at")),
        ("started", t.get("started_at") or "-"), ("finished", t.get("finished_at") or "-"),
    ])
    if t.get("description"):
        out.line("description:")
        for ln in str(t["description"]).splitlines():
            out.line(f"  {ln}")
    for label, key in (("acceptance criteria", "acceptance_criteria"), ("verify commands", "verify_commands")):
        items = t.get(key) or []
        if items:
            out.line(f"{label}:")
            for i in items:
                out.line(f"  - {i}")


def render_task_detail(out: Out, d: dict[str, Any]) -> None:
    render_task(out, d["task"])
    runs = d.get("runs") or []
    if runs:
        out.line("")
        out.table(
            ["run", "phase", "provider/model", "status", "started"],
            [[r["id"], r.get("phase"), f"{r.get('provider')}/{r.get('model') or '?'}", r.get("status"), r.get("started_at")] for r in runs],
            title="runs", keep=["run"],
        )
    ver = d.get("verification") or []
    if ver:
        last = ver[-1]
        out.line("")
        out.line(f"latest verification: {last.get('status')}")
        for c in last.get("checks") or []:
            out.line(f"  [{'ok' if c.get('passed') else 'FAIL'}] {c.get('name')}" + (f" - {c['error']}" if c.get("error") else ""))


# ---- projects --------------------------------------------------------------------------------------
@projects_app.command("list")
def projects_list(ctx: typer.Context, json_out: JsonOpt = False, url: UrlOpt = None) -> None:
    """List registered projects.

    Example: orchestrator projects list
    """
    with session(ctx, json_out, url) as s:
        s.show(s.api.get("/api/projects"), render_projects)


@projects_app.command("add")
def projects_add(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Display name.")],
    path: Annotated[Path, typer.Argument(help="Repository folder (resolved to an absolute path).")],
    verify_cmd: VerifyOpt = None,
    json_out: JsonOpt = False,
    url: UrlOpt = None,
) -> None:
    """Register a project folder. --verify-cmd (repeatable) are the project's default checks.

    Example: orchestrator projects add shop D:\\code\\shop --verify-cmd "npm test"
    """
    with session(ctx, json_out, url) as s:
        body: dict[str, Any] = {"name": name, "path": str(path.expanduser().resolve())}
        if verify_cmd:
            body["verify_commands"] = list(verify_cmd)
        s.show(s.api.post("/api/projects", body), render_project)


@projects_app.command("show")
def projects_show(ctx: typer.Context, project_id: Annotated[str, typer.Argument()], json_out: JsonOpt = False, url: UrlOpt = None) -> None:
    """Show one project.

    Example: orchestrator projects show prj_1234abcd
    """
    with session(ctx, json_out, url) as s:
        s.show(s.api.get(f"/api/projects/{project_id}"), render_project)


@projects_app.command("remove")
def projects_remove(ctx: typer.Context, project_id: Annotated[str, typer.Argument()], json_out: JsonOpt = False, url: UrlOpt = None) -> None:
    """Remove a project from the orchestrator (the folder on disk is not touched).

    Example: orchestrator projects remove prj_1234abcd
    """
    with session(ctx, json_out, url) as s:
        s.api.delete(f"/api/projects/{project_id}")
        s.show({"removed": project_id}, lambda o, d: o.line(f"removed project {project_id}"))


# ---- tasks -----------------------------------------------------------------------------------------
@tasks_app.command("list")
def tasks_list(
    ctx: typer.Context,
    project: ProjectOpt = None,
    state: Annotated[Optional[str], typer.Option("--state", help="Comma-separated states, e.g. RUNNING,QUEUED.")] = None,
    json_out: JsonOpt = False,
    url: UrlOpt = None,
) -> None:
    """List tasks, optionally by project and/or state.

    Example: orchestrator tasks list --project prj_1234abcd --state FAILED,BLOCKED
    """
    with session(ctx, json_out, url) as s:
        s.show(s.api.get("/api/tasks", project_id=project, state=state), render_tasks)


@tasks_app.command("show")
def tasks_show(ctx: typer.Context, task_id: Annotated[str, typer.Argument()], json_out: JsonOpt = False, url: UrlOpt = None) -> None:
    """Show a task with its runs and latest verification (--json: {task, runs, verification}).

    Example: orchestrator tasks show tsk_1234abcd
    """
    with session(ctx, json_out, url) as s:
        task = s.api.get(f"/api/tasks/{task_id}")
        data = {
            "task": task,
            "runs": s.api.get(f"/api/tasks/{task_id}/runs"),
            "verification": s.api.get(f"/api/tasks/{task_id}/verification"),
        }
        s.show(data, render_task_detail)


@tasks_app.command("create")
def tasks_create(
    ctx: typer.Context,
    project: ProjectOpt = None, title: TitleOpt = None, description: DescOpt = None, description_file: DescFileOpt = None,
    task_type: TypeOpt = None, role: RoleOpt = None, priority: PriorityOpt = None, depends_on: DependsOpt = None,
    criteria: CriteriaOpt = None, verify_cmd: VerifyOpt = None, provider: ProviderOpt = None, model: ModelOpt = None,
    json_out: JsonOpt = False, url: UrlOpt = None,
) -> None:
    """Create a task (it is NOT started; use `orchestrator run <id>`).

    Example: orchestrator tasks create --project prj_1234abcd --title "Fix login bug"
      --description-file spec.md --verify-cmd "npm test" --criteria "login works"
    """
    body = build_task_body(project, title, description, description_file, task_type, role, priority, depends_on, criteria, verify_cmd, provider, model)
    with session(ctx, json_out, url) as s:
        s.show(s.api.post("/api/tasks", body), render_task)


# ---- run / resume / pause / cancel -----------------------------------------------------------------
def follow_task(s: Session, task_id: str, after_seq: int, *, raw: bool = False, filter_types: bool = True) -> dict[str, Any]:
    """Print the task's events live until it stops; returns the final task JSON."""

    def emit(e: dict[str, Any]) -> None:
        if s.json:
            s.out.json_line(e)
            return
        if filter_types and not raw and e.get("type") not in follow_mod.LOG_TYPES:
            return
        for ln in follow_mod.format_event(e, raw=raw):
            s.out.line(ln)
        if e.get("type") == "approval.requested":
            a = (e.get("payload") or {}).get("approval") or {}
            if a.get("id") and a.get("status", "pending") == "pending":
                s.out.line(f"-> waiting for a decision: orchestrator approvals approve {a['id']}  (or: deny {a['id']})")

    return follow_mod.follow(s.api, task_id, after_seq, emit)


def _finish_follow(s: Session, final: dict[str, Any]) -> None:
    state = final.get("state")
    if s.json:
        s.out.json_line({"type": "cli.result", "task": final})
    else:
        reason = f" ({final['state_reason']})" if final.get("state_reason") else ""
        s.out.line(f"task {final.get('id')} finished: {state}{reason}")
    if state != "COMPLETED":
        raise typer.Exit(code=1)


def run(
    ctx: typer.Context,
    task_id: Annotated[Optional[str], typer.Argument(help="Existing task id to start.")] = None,
    project: ProjectOpt = None, title: TitleOpt = None, description: DescOpt = None, description_file: DescFileOpt = None,
    task_type: TypeOpt = None, role: RoleOpt = None, priority: PriorityOpt = None, depends_on: DependsOpt = None,
    criteria: CriteriaOpt = None, verify_cmd: VerifyOpt = None, provider: ProviderOpt = None, model: ModelOpt = None,
    follow: Annotated[bool, typer.Option("--follow", help="Stream events until the task completes, fails or blocks.")] = False,
    json_out: JsonOpt = False, url: UrlOpt = None,
) -> None:
    """Start an existing task, or create + start one in a single step.

    With --follow the events stream live (WebSocket, REST polling as fallback) and the exit code is 0 only if
    the task ends COMPLETED (1 for FAILED / BLOCKED / CANCELLED / PAUSED). With --json --follow every event
    is printed as one JSON line, followed by {"type": "cli.result", "task": {...}}.

    \b

    Examples:
      orchestrator run tsk_1234abcd
      orchestrator run --project prj_1234abcd --title "Add dark mode" --verify-cmd "npm test" --follow
    """
    creating = any(v not in (None, [], False) for v in (project, title, description, description_file, task_type, role, priority, depends_on, criteria, verify_cmd, provider, model))
    if task_id and creating:
        raise fail("give either a task id or --project/--title (create + start), not both")
    if not task_id and not (project and title):
        raise fail("give a task id, or --project and --title to create and start a new task")
    body = None if task_id else build_task_body(project, title, description, description_file, task_type, role, priority, depends_on, criteria, verify_cmd, provider, model)
    with session(ctx, json_out, url) as s:
        if body is not None:
            task = s.api.post("/api/tasks", body)
            task_id = task["id"]
            if not s.json:
                s.out.line(f"created task {task_id}")
        status = s.api.get("/api/status")
        after = int((status or {}).get("last_seq", 0))
        try:
            started = s.api.post(f"/api/tasks/{task_id}/start")
        except ApiError:
            if body is not None:
                s.out.note(f"task {task_id} was created but could not be started; start it later with `orchestrator run {task_id}`")
            raise
        if not follow:
            s.show(started, lambda o, t: o.line(f"started task {t['id']}: state {t['state']}"))
            return
        if not s.json:
            s.out.line(f"started task {task_id}: state {started['state']}; following (Ctrl+C stops following, the task keeps running)")
        try:
            final = follow_task(s, task_id, after)
        except KeyboardInterrupt:
            s.out.note(f"stopped following; task {task_id} keeps running (orchestrator logs {task_id} --follow)")
            raise typer.Exit(code=130) from None
        _finish_follow(s, final)


def resume(ctx: typer.Context, task_id: Annotated[str, typer.Argument()], json_out: JsonOpt = False, url: UrlOpt = None) -> None:
    """Resume a PAUSED task, or retry a FAILED / BLOCKED one (the output says which).

    Example: orchestrator resume tsk_1234abcd
    """
    with session(ctx, json_out, url) as s:
        state = (s.api.get(f"/api/tasks/{task_id}") or {}).get("state")
        if state == "PAUSED":
            action, path = "resumed", "resume"
        elif state in ("FAILED", "BLOCKED"):
            action, path = "retried", "retry"
        else:
            raise fail(f"task {task_id} is {state}; only PAUSED (resume) or FAILED / BLOCKED (retry) tasks can be resumed")
        task = s.api.post(f"/api/tasks/{task_id}/{path}")
        s.show({"action": action, "task": task}, lambda o, d: o.line(f"{action} task {task_id}: now {d['task']['state']}"))


def _control(verb: str, past: str):
    def cmd(ctx: typer.Context, task_id: Annotated[str, typer.Argument()], json_out: JsonOpt = False, url: UrlOpt = None) -> None:
        with session(ctx, json_out, url) as s:
            task = s.api.post(f"/api/tasks/{task_id}/{verb}")
            s.show(task, lambda o, t: o.line(f"{past} task {task_id}: now {t['state']}"))

    return cmd


pause = _control("pause", "paused")
pause.__doc__ = """Pause a running or queued task (the current run is stopped cleanly).

    Example: orchestrator pause tsk_1234abcd
    """
cancel = _control("cancel", "cancelled")
cancel.__doc__ = """Cancel a task permanently (its run is terminated).

    Example: orchestrator cancel tsk_1234abcd
    """


# ---- logs ------------------------------------------------------------------------------------------
def logs(
    ctx: typer.Context,
    task_id: Annotated[str, typer.Argument()],
    follow: Annotated[bool, typer.Option("--follow", help="Keep streaming until the task stops.")] = False,
    since_seq: Annotated[int, typer.Option("--since-seq", min=0, help="Only events after this sequence number.")] = 0,
    raw: Annotated[bool, typer.Option("--raw", help="Print every event as one JSON line (nothing filtered).")] = False,
    json_out: JsonOpt = False, url: UrlOpt = None,
) -> None:
    """Show a task's output, tool calls, state changes and verification results with timestamps.

    \b

    Examples:
      orchestrator logs tsk_1234abcd
      orchestrator logs tsk_1234abcd --follow --since-seq 120
    """
    with session(ctx, json_out, url) as s:
        s.api.get(f"/api/tasks/{task_id}")  # 404 early
        if follow:
            final = follow_task(s, task_id, since_seq, raw=raw, filter_types=True)
            if not s.json:
                s.out.line(f"-- task {task_id} is {final.get('state')}")
            return
        events: list[dict[str, Any]] = []
        last = since_seq
        while True:
            batch = s.api.get(f"/api/tasks/{task_id}/events", after_seq=last, limit=5000) or []
            events.extend(batch)
            if len(batch) < 5000:
                break
            last = batch[-1]["seq"]
        if s.json:
            s.out.json(events)
            return
        shown = 0
        for e in events:
            if not raw and e.get("type") not in follow_mod.LOG_TYPES:
                continue
            for ln in follow_mod.format_event(e, raw=raw):
                s.out.line(ln)
            shown += 1
        if not shown:
            s.out.line("(no log events yet)")


# ---- approvals -------------------------------------------------------------------------------------
def render_approvals(out: Out, data: list[dict[str, Any]]) -> None:
    out.table(
        ["id", "status", "kind", "task", "requested", "summary"],
        [[a["id"], a["status"], a["kind"], a.get("task_id") or "-", a.get("requested_at"), short(a["summary"], 90)] for a in data],
        keep=["id", "task"],
    )


@approvals_app.command("list")
def approvals_list(
    ctx: typer.Context,
    status: Annotated[str, typer.Option("--status", help="pending (default), approved, denied or all.")] = "pending",
    task: Annotated[Optional[str], typer.Option("--task", help="Only this task id.")] = None,
    json_out: JsonOpt = False, url: UrlOpt = None,
) -> None:
    """List approval requests (pending by default).

    Example: orchestrator approvals list --status all
    """
    if status not in ("pending", "approved", "denied", "all"):
        raise fail("--status must be pending, approved, denied or all")
    with session(ctx, json_out, url) as s:
        s.show(s.api.get("/api/approvals", status=None if status == "all" else status, task_id=task), render_approvals)


def _resolve(approve: bool):
    def cmd(
        ctx: typer.Context,
        approval_id: Annotated[str, typer.Argument()],
        note: Annotated[Optional[str], typer.Option("--note", help="Optional note stored with the decision.")] = None,
        json_out: JsonOpt = False, url: UrlOpt = None,
    ) -> None:
        with session(ctx, json_out, url) as s:
            body: dict[str, Any] = {"approve": approve}
            if note:
                body["note"] = note
            a = s.api.post(f"/api/approvals/{approval_id}/resolve", body)
            s.show(a, lambda o, d: o.line(f"{d['status']}: {d['id']} ({short(d['summary'], 100)})"))

    return cmd


approvals_approve = _resolve(True)
approvals_approve.__doc__ = """Approve a pending request.

    Example: orchestrator approvals approve apr_1234abcd --note "looks fine"
    """
approvals_deny = _resolve(False)
approvals_deny.__doc__ = """Deny a pending request.

    Example: orchestrator approvals deny apr_1234abcd --note "not now"
    """
approvals_app.command("approve")(approvals_approve)
approvals_app.command("deny")(approvals_deny)
