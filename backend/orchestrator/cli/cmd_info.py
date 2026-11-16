"""Read-mostly commands: status, providers, models, usage, skills, routing, leader/advisor, knowledge, analytics."""

from __future__ import annotations

from typing import Annotated, Any, Optional

import typer

from orchestrator.cli.client import Result
from orchestrator.cli.common import JsonOpt, UrlOpt, fail, optional_endpoint, session
from orchestrator.cli.output import Out, measured, render_generic, short

providers_app = typer.Typer(help="Provider CLIs (claude, codex, antigravity): detection, auth and usage state.", invoke_without_command=True)
skills_app = typer.Typer(help="Skills & MCP catalog: install, activate per project, update, roll back.", no_args_is_help=True)
routing_app = typer.Typer(help="Model routing: dry-run a task and inspect past decisions.", no_args_is_help=True)
leader_app = typer.Typer(help="The leader model that coordinates tasks.", no_args_is_help=True)
advisor_app = typer.Typer(help="The advisor model consulted on hard problems.", no_args_is_help=True)
knowledge_app = typer.Typer(help="Project knowledge base.", no_args_is_help=True)
analytics_app = typer.Typer(help="Run, success and routing analytics.", no_args_is_help=True)

ACTIVE_STATES = ("PLANNING", "RUNNING", "TESTING", "REVIEWING")


# ---- status ----------------------------------------------------------------------------------------
def render_status(out: Out, d: dict[str, Any]) -> None:
    h, st = d["health"], d["status"]
    counts = st.get("task_counts") or {}
    running = sum(counts.get(k, 0) for k in ACTIVE_STATES)
    sched = st.get("scheduler") or {}
    leader = st.get("leader")
    out.kv([
        ("health", h.get("status")),
        ("version", st.get("version") or h.get("version")),
        ("uptime", measured(st.get("uptime_seconds"))),
        ("scheduler", ("running" if sched.get("running") else "stopped") + (", assignment paused" if sched.get("assignment_paused") else "")),
        ("leader", f"{leader['provider']}/{leader.get('model') or '?'}" if leader else "not set"),
        ("projects", st.get("projects")),
        ("tasks running", running),
        ("tasks queued", counts.get("QUEUED", 0)),
        ("tasks blocked", counts.get("BLOCKED", 0)),
        ("live runs", st.get("live_runs")),
        ("pending approvals", st.get("pending_approvals")),
        ("last event seq", st.get("last_seq")),
        ("providers", ", ".join(st.get("providers") or []) or "none"),
    ])
    if counts:
        out.line("task states: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


def status(ctx: typer.Context, json_out: JsonOpt = False, url: UrlOpt = None) -> None:
    """Show backend health, scheduler, leader, task counts and pending approvals.

    Example: orchestrator status
    """
    with session(ctx, json_out, url) as s:
        health = s.api.health()
        s.show({"health": health, "status": s.api.get("/api/status")}, render_status)


# ---- providers / models / usage --------------------------------------------------------------------
def render_providers(out: Out, data: list[dict[str, Any]]) -> None:
    rows = []
    for p in data:
        auth = p.get("authenticated")
        us = (p.get("usage") or {}).get("state")
        rows.append([
            p["provider"], "yes" if p.get("installed") else "no", p.get("version") or "unknown",
            "unknown" if auth is None else ("yes" if auth else "no"), p.get("billing_class") or "unknown",
            p.get("model_count", 0), us or "unknown", short(p.get("error") or "", 60),
        ])
    out.table(["provider", "installed", "version", "logged in", "billing", "models", "usage", "note"], rows)


@providers_app.callback()
def providers_list(ctx: typer.Context, json_out: JsonOpt = False, url: UrlOpt = None) -> None:
    """List providers with detection, authentication and usage state. `providers refresh` re-detects them.

    Example: orchestrator providers
    """
    if ctx.invoked_subcommand is not None:
        return
    with session(ctx, json_out, url) as s:
        s.show(s.api.get("/api/providers"), render_providers)


@providers_app.command("refresh")
def providers_refresh(
    ctx: typer.Context,
    provider: Annotated[Optional[str], typer.Option("--provider", help="Only re-detect this provider.")] = None,
    json_out: JsonOpt = False, url: UrlOpt = None,
) -> None:
    """Re-run provider detection and model discovery (no model call, no quota spent).

    Example: orchestrator providers refresh --provider codex
    """
    with session(ctx, json_out, url) as s:
        s.show(s.api.post("/api/providers/refresh", {"provider": provider} if provider else None), render_providers)


def models(ctx: typer.Context, provider: Annotated[Optional[str], typer.Option("--provider", help="Only this provider.")] = None,
           json_out: JsonOpt = False, url: UrlOpt = None) -> None:
    """List known models with tier, capabilities and billing class.

    Example: orchestrator models --provider claude
    """
    with session(ctx, json_out, url) as s:
        data = s.api.get("/api/models") or []
        if provider:
            data = [m for m in data if m.get("provider") == provider]

        def render(out: Out, rows: list[dict[str, Any]]) -> None:
            out.table(
                ["provider", "model", "alias", "tier", "billing", "capabilities", "discovered"],
                [[m["provider"], m["model_id"], m.get("alias") or "-", m.get("tier") or "unknown", m.get("billing_class") or "unknown",
                  ",".join(m.get("capabilities") or []), "yes" if m.get("discovered") else "no"] for m in rows],
            )

        s.show(data, render)


def render_usage(out: Out, data: list[dict[str, Any]]) -> None:
    states, values = [], []
    for e in data:
        us = e.get("usage_state") or {}
        scope = f"{e['provider']}/{e['model']}" if e.get("model") else e["provider"]
        states.append([scope, us.get("state") or "unknown", us.get("confidence") or "unknown", us.get("resets_at") or "-",
                       short(us.get("reason") or "", 70)])
        for v in e.get("values") or []:
            val = v.get("value")
            values.append([scope, v.get("metric"), "unknown" if val is None else val, v.get("unit") or "-",
                           v.get("exactness") or "unknown", v.get("source") or "-", v.get("observed_at") or "-"])
    out.table(["scope", "state", "confidence", "resets at", "reason"], states, title="usage state")
    out.line("")
    if values:
        out.table(["scope", "metric", "value", "unit", "exactness", "source", "observed"], values, title="observed values")
    else:
        out.line("observed values: unknown (no usage has been reported by the providers yet)")


def usage(ctx: typer.Context, json_out: JsonOpt = False, url: UrlOpt = None) -> None:
    """Show provider usage state and the observed values (with exactness). Missing data is shown as unknown.

    Example: orchestrator usage
    """
    with session(ctx, json_out, url) as s:
        s.show(s.api.get("/api/usage"), render_usage)


# ---- skills ----------------------------------------------------------------------------------------
def render_skills(out: Out, data: list[dict[str, Any]]) -> None:
    out.table(
        ["id", "kind", "category", "state", "installable", "active in", "name"],
        [[k["id"], k.get("kind"), k.get("category"), k.get("state"), "yes" if k.get("installable") else "no",
          len(k.get("active_projects") or []), short(k.get("name"), 40)] for k in data],
        keep=["id"],
    )


def render_skill_outcome(out: Out, d: dict[str, Any]) -> None:
    o = d.get("outcome") or {}
    sk = d.get("skill") or {}
    status_ = o.get("status") or ("ok" if o.get("ok", True) else "failed")
    out.line(f"{o.get('action', 'done')}: {status_}" + (f" - {o['note']}" if o.get("note") else ""))
    if o.get("message"):
        out.line(str(o["message"]))
    out.line(f"skill {sk.get('id', '?')} is now {sk.get('state', 'unknown')}")


@skills_app.command("list")
def skills_list(
    ctx: typer.Context,
    query: Annotated[Optional[str], typer.Option("--query", "-q", help="Search id, name and description.")] = None,
    category: Annotated[Optional[str], typer.Option("--category")] = None,
    kind: Annotated[Optional[str], typer.Option("--kind", help="e.g. skill, mcp.")] = None,
    state: Annotated[Optional[str], typer.Option("--state")] = None,
    project: Annotated[Optional[str], typer.Option("--project", help="Show activation info for this project.")] = None,
    json_out: JsonOpt = False, url: UrlOpt = None,
) -> None:
    """List catalog skills / MCP servers with their state.

    Example: orchestrator skills list --kind skill --state installed
    """
    with session(ctx, json_out, url) as s:
        s.show(s.api.get("/api/skills", q=query, category=category, kind=kind, state=state, project_id=project), render_skills)


@skills_app.command("show")
def skills_show(ctx: typer.Context, skill_id: Annotated[str, typer.Argument()], json_out: JsonOpt = False, url: UrlOpt = None) -> None:
    """Show one skill's metadata, security notes and event history.

    Example: orchestrator skills show some-skill-id
    """
    with session(ctx, json_out, url) as s:
        s.show(s.api.get(f"/api/skills/{skill_id}"), lambda o, d: render_generic(o, d))


def _skill_action(ctx: typer.Context, skill_id: str, action: str, body: Optional[dict[str, Any]], json_out: bool, url: Optional[str]) -> None:
    with session(ctx, json_out, url) as s:
        res: Result = s.api.call("POST", f"/api/skills/{skill_id}/{action}", json=body if body is not None else {})
        data = res.data

        def render(out: Out, d: dict[str, Any]) -> None:
            render_skill_outcome(out, d)
            o = d.get("outcome") or {}
            if res.status == 202 and o.get("approval_id"):
                aid = o["approval_id"]
                out.line("")
                out.line(f"approval required (id {aid}); nothing was activated yet.")
                out.line(f"  1. orchestrator approvals approve {aid}")
                out.line(f"  2. orchestrator skills activate {skill_id} --project {o.get('project_id') or '<project-id>'}")

        s.show(data, render)


@skills_app.command("install")
def skills_install(ctx: typer.Context, skill_id: Annotated[str, typer.Argument()], json_out: JsonOpt = False, url: UrlOpt = None) -> None:
    """Fetch the pinned version of a skill into the local store (not yet active in any project).

    Example: orchestrator skills install some-skill-id
    """
    _skill_action(ctx, skill_id, "install", None, json_out, url)


@skills_app.command("activate")
def skills_activate(
    ctx: typer.Context, skill_id: Annotated[str, typer.Argument()],
    project: Annotated[str, typer.Option("--project", help="Project id to activate it in.")],
    target: Annotated[Optional[list[str]], typer.Option("--target", help="claude and/or codex (default: claude).")] = None,
    json_out: JsonOpt = False, url: UrlOpt = None,
) -> None:
    """Activate a skill in a project. An untrusted install needs an approval first (printed with how to approve).

    Example: orchestrator skills activate some-skill-id --project prj_1234abcd
    """
    body: dict[str, Any] = {"project_id": project}
    if target:
        body["targets"] = list(target)
    _skill_action(ctx, skill_id, "activate", body, json_out, url)


@skills_app.command("deactivate")
def skills_deactivate(
    ctx: typer.Context, skill_id: Annotated[str, typer.Argument()],
    project: Annotated[str, typer.Option("--project", help="Project id to deactivate it in.")],
    json_out: JsonOpt = False, url: UrlOpt = None,
) -> None:
    """Remove a skill's activation from a project.

    Example: orchestrator skills deactivate some-skill-id --project prj_1234abcd
    """
    _skill_action(ctx, skill_id, "deactivate", {"project_id": project}, json_out, url)


@skills_app.command("update")
def skills_update(ctx: typer.Context, skill_id: Annotated[str, typer.Argument()],
                  ref: Annotated[Optional[str], typer.Option("--ref", help="Pin to this ref instead of the catalog's.")] = None,
                  json_out: JsonOpt = False, url: UrlOpt = None) -> None:
    """Update a skill to its newest pinned version (the previous version is kept for rollback).

    Example: orchestrator skills update some-skill-id
    """
    _skill_action(ctx, skill_id, "update", {"ref": ref} if ref else None, json_out, url)


@skills_app.command("rollback")
def skills_rollback(ctx: typer.Context, skill_id: Annotated[str, typer.Argument()], json_out: JsonOpt = False, url: UrlOpt = None) -> None:
    """Return a skill to its previous version.

    Example: orchestrator skills rollback some-skill-id
    """
    _skill_action(ctx, skill_id, "rollback", None, json_out, url)


# ---- routing ---------------------------------------------------------------------------------------
def render_preview(out: Out, d: dict[str, Any]) -> None:
    c, dec = d.get("classification") or {}, d.get("decision") or {}
    out.kv([
        ("outcome", d.get("outcome")),
        ("chosen", f"{dec['provider']}/{dec.get('model') or '?'} (tier {dec.get('tier') or 'unknown'})" if dec.get("provider") else "none"),
        ("task type", c.get("task_type")), ("complexity", c.get("complexity")), ("risk", c.get("risk")),
        ("role", dec.get("role") or "-"), ("phase", dec.get("phase")),
        ("needs capabilities", ", ".join(dec.get("required_capabilities") or []) or "-"),
    ])
    if dec.get("no_candidate_reason"):
        out.line(f"no candidate: {dec['no_candidate_reason']}")
    for r in dec.get("reasons") or []:
        out.line(f"  - {r}")
    cands = dec.get("candidates") or []
    if cands:
        out.line("")
        out.table(["rank", "provider", "model", "tier", "score", "usage"],
                  [[x["rank"], x["provider"], x["model"], x["tier"], x["score"], x.get("usage_state")] for x in cands], title="candidates")
    rej = dec.get("rejected") or []
    if rej:
        out.line("")
        out.table(["provider", "model", "rejected by", "detail"],
                  [[x["provider"], x["model"], x["filter"], short(x.get("detail"), 70)] for x in rej], title="rejected")


@routing_app.command("preview")
def routing_preview(
    ctx: typer.Context,
    title: Annotated[str, typer.Option("--title", help="Task title.")],
    description: Annotated[str, typer.Option("--description")] = "",
    task_type: Annotated[Optional[str], typer.Option("--type")] = None,
    role: Annotated[Optional[str], typer.Option("--role")] = None,
    phase: Annotated[str, typer.Option("--phase", help="plan, execute (default), repair, review or advise.")] = "execute",
    json_out: JsonOpt = False, url: UrlOpt = None,
) -> None:
    """Dry-run the router for a task: which model would be chosen and why. Creates nothing, runs nothing.

    Example: orchestrator routing preview --title "Refactor the auth module" --type coding
    """
    body: dict[str, Any] = {"title": title, "description": description, "phase": phase}
    if task_type:
        body["task_type"] = task_type
    if role:
        body["role"] = role
    with session(ctx, json_out, url) as s:
        s.show(s.api.post("/api/routing/preview", body), render_preview)


@routing_app.command("decisions")
def routing_decisions(
    ctx: typer.Context,
    task: Annotated[Optional[str], typer.Option("--task", help="Only this task id.")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=500)] = 50,
    json_out: JsonOpt = False, url: UrlOpt = None,
) -> None:
    """List recent routing decisions.

    Example: orchestrator routing decisions --task tsk_1234abcd
    """
    def render(out: Out, rows: list[dict[str, Any]]) -> None:
        out.table(
            ["when", "task", "phase", "chosen", "reason"],
            [[str(r.get("created_at"))[:19], r["task_id"], r["phase"], f"{r['chosen_provider']}/{r.get('chosen_model') or '?'}",
              short("; ".join(r.get("reasons") or []), 80)] for r in rows],
            keep=["task"],
        )

    with session(ctx, json_out, url) as s:
        s.show(s.api.get("/api/routing/decisions", task_id=task, limit=limit), render)


# ---- leader / advisor ------------------------------------------------------------------------------
def render_leader(out: Out, d: dict[str, Any]) -> None:
    ld, adv = d.get("leader"), d.get("advisor")
    out.kv([
        ("leader", f"{ld['provider']}/{ld.get('model') or '?'} (tier {ld.get('tier') or 'unknown'})" if ld else "not set"),
        ("advisor", f"{adv['provider']}/{adv.get('model') or '?'}" if adv else "none"),
        ("advisor in effect", d.get("advisor_effective")),
        ("assignment paused", d.get("assignment_paused")), ("switch in progress", d.get("switch_in_progress")),
    ])
    st = d.get("settings") or {}
    if st:
        out.line("settings:")
        out.kv([(k, "unknown" if v is None else v) for k, v in st.items() if not isinstance(v, (dict, list))], indent=2)


@leader_app.command("show")
def leader_show(ctx: typer.Context, json_out: JsonOpt = False, url: UrlOpt = None) -> None:
    """Show the current leader, advisor and leadership settings.

    Example: orchestrator leader show
    """
    with session(ctx, json_out, url) as s:
        optional_endpoint(s, lambda: s.api.get("/api/leadership"), render_leader, "leader")


@leader_app.command("set")
def leader_set(
    ctx: typer.Context,
    provider: Annotated[str, typer.Option("--provider", "-p")],
    model: Annotated[str, typer.Option("--model", "-m")],
    json_out: JsonOpt = False, url: UrlOpt = None,
) -> None:
    """Switch the leader safely (the new leader must validate the coordination state; otherwise it rolls back).

    Example: orchestrator leader set --provider claude --model opus
    """
    def render(out: Out, d: dict[str, Any]) -> None:
        out.line(f"leader switch: {d.get('status')}")
        render_generic(out, {k: v for k, v in d.items() if k != "status"}, indent=2)

    with session(ctx, json_out, url) as s:
        optional_endpoint(s, lambda: s.api.post("/api/leadership/switch", {"provider": provider, "model": model}, timeout=600), render, "leader switch")


@advisor_app.command("consult")
def advisor_consult(
    ctx: typer.Context,
    task: Annotated[Optional[str], typer.Option("--task", help="Task id the question is about.")] = None,
    problem: Annotated[str, typer.Option("--problem", help="What you want advice on.")] = "",
    json_out: JsonOpt = False, url: UrlOpt = None,
) -> None:
    """Ask the advisor model a question (spends model quota; the answer is recorded as a decision).

    Example: orchestrator advisor consult --task tsk_1234abcd --problem "Which migration order is safest?"
    """
    if not problem.strip():
        raise fail("--problem is required")
    body: dict[str, Any] = {"problem": problem}
    if task:
        body["task_id"] = task
    with session(ctx, json_out, url) as s:
        optional_endpoint(s, lambda: s.api.post("/api/advisor/consult", body, timeout=600), lambda o, d: render_generic(o, d), "advisor")


# ---- knowledge / analytics -------------------------------------------------------------------------
def render_search(out: Out, d: dict[str, Any]) -> None:
    results = d.get("results") or []
    if not results:
        out.line("(no matches)")
        return
    for i, r in enumerate(results, 1):
        out.line(f"{i}. [{r.get('kind')}] {r.get('title')}  ({r.get('source') or r.get('source_path') or '-'})")
        text = str(r.get("text") or "").strip().replace("\n", " ")
        if text:
            out.line(f"     {short(text, 200)}")


@knowledge_app.command("search")
def knowledge_search(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help="Search text.")],
    project: Annotated[Optional[str], typer.Option("--project")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=50)] = 8,
    json_out: JsonOpt = False, url: UrlOpt = None,
) -> None:
    """Search the indexed project knowledge (docs, ADRs, past errors, ...).

    Example: orchestrator knowledge search "token refresh" --project prj_1234abcd --limit 5
    """
    body: dict[str, Any] = {"query": query, "limit": limit}
    if project:
        body["project_id"] = project
    with session(ctx, json_out, url) as s:
        optional_endpoint(s, lambda: s.api.post("/api/knowledge/search", body), render_search, "knowledge")


@analytics_app.command("summary")
def analytics_summary(ctx: typer.Context, json_out: JsonOpt = False, url: UrlOpt = None) -> None:
    """Show aggregate run and success analytics (every figure carries its exactness; none are invented).

    Example: orchestrator analytics summary
    """
    with session(ctx, json_out, url) as s:
        optional_endpoint(s, lambda: s.api.get("/api/analytics/summary"), lambda o, d: render_generic(o, d), "analytics")

