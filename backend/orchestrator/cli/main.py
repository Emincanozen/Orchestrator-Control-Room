"""`orchestrator` command line.

`serve` runs the backend in the foreground; `dashboard` / `stop` manage a detached background copy. Every other
command is a thin HTTP client of the running backend (see orchestrator.cli.client) and keeps no state.

Exit codes: 0 ok, 1 API or usage error, 2 backend unreachable.
"""

from __future__ import annotations

import ipaddress
import os
import threading
import webbrowser
from typing import Annotated, Any, Optional

import click
import typer

from orchestrator import __version__
from orchestrator.cli import cmd_info, cmd_work, doctor as doctor_mod, server
from orchestrator.cli.client import CliError, is_running, read_token
from orchestrator.cli.common import JsonOpt, UrlOpt, session
from orchestrator.cli.output import Out
from orchestrator.config import FRONTEND_DIST, effective_policies

# Documented exit codes: 1 = usage/API error (click's default for usage errors is 2, reserved here for "unreachable").
def _usage_errors_exit_1() -> None:
    click.exceptions.UsageError.exit_code = 1
    try:  # newer Typer versions vendor their own copy of click
        from typer import _click as vendored  # type: ignore[attr-defined]

        vendored.exceptions.UsageError.exit_code = 1
    except (ImportError, AttributeError):
        pass


_usage_errors_exit_1()

app = typer.Typer(
    help="AI Orchestrator command line. Start the backend with `orchestrator dashboard` (or `serve`), then drive it "
         "from here or from the dashboard; both use the same API and database.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    add_completion=False,
)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _security() -> dict[str, Any]:
    return effective_policies({}).security


def _version(value: bool) -> None:
    if value:
        typer.echo(f"orchestrator {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    json_out: JsonOpt = False,
    url: UrlOpt = None,
    version: Annotated[bool, typer.Option("--version", callback=_version, is_eager=True, help="Show the version.")] = False,
) -> None:
    """AI Orchestrator command line (global options may also be given per command)."""
    ctx.obj = {"json": json_out, "url": url}


# ---- serve -----------------------------------------------------------------------------------------
def _open_when_ready(host: str, port: int) -> None:
    base = f"http://{host}:{port}"

    def run() -> None:
        if server.wait_until(lambda: is_running(base), timeout=60):
            try:
                webbrowser.open(f"{base}/#token={read_token()}")
            except Exception:  # noqa: BLE001 - never break the server over a browser problem
                pass

    threading.Thread(target=run, name="open-browser", daemon=True).start()


def _probe_instance_lock() -> None:
    """Fail with one clear line (not a traceback from inside the app lifespan) when the data dir is in use."""
    from orchestrator.config import get_paths
    from orchestrator.security.instance_lock import InstanceLock, InstanceLockError

    try:
        InstanceLock(get_paths().data_dir).acquire().release()
    except InstanceLockError as e:
        Out().error(str(e))
        raise typer.Exit(code=1) from None


@app.command()
def serve(
    host: Annotated[Optional[str], typer.Option(help="Bind address. Default from policies.yaml (127.0.0.1); loopback only.")] = None,
    port: Annotated[Optional[int], typer.Option(help="Port. Default from policies.yaml (8765).")] = None,
    log_level: Annotated[str, typer.Option(help="uvicorn log level.")] = "info",
    browser: Annotated[bool, typer.Option("--browser/--no-browser", help="Open the dashboard in the browser once the server is up (default: do not).")] = False,
    access_log: Annotated[bool, typer.Option("--access-log/--no-access-log", help="Log every HTTP request. The background server runs with --no-access-log because WebSocket URLs carry the token.")] = True,
    expose: Annotated[bool, typer.Option("--i-understand-exposing-the-dashboard", help="Allow a non-loopback --host (not recommended).")] = False,
) -> None:
    """Run the API and the built dashboard in the foreground (Ctrl+C stops it).

    The server is bound to 127.0.0.1 by default and a non-loopback address is refused: the API can start agents
    that edit files and run commands, so it must not be reachable from the network.

    \b

    Examples:
      orchestrator serve
      orchestrator serve --port 9000 --no-browser
    """
    import uvicorn

    from orchestrator.security import sandbox
    from orchestrator.security.logfilter import uvicorn_log_config

    try:  # N2 re-review S7: never elevated, no override (nothing here needs administrator rights)
        sandbox.refuse_elevated()
    except (sandbox.ElevatedProcess, sandbox.SandboxUnavailable) as exc:
        Out().error(str(exc))
        raise typer.Exit(code=1) from None
    sec = _security()
    host = host or str(sec.get("bind_host", "127.0.0.1"))
    port = port or int(sec.get("port", 8765))
    if not _is_loopback(host):
        if not expose:
            Out().error(
                f"refusing to bind {host!r}: the orchestrator API can run agents that modify files and execute commands, "
                f"so it only listens on loopback (127.0.0.1) by default. If you really need it, repeat the command with "
                f"--i-understand-exposing-the-dashboard (the bearer token is then the only barrier on your network)."
            )
            raise typer.Exit(code=1)
        Out().note(f"WARNING: binding {host}; anyone who can reach this port and obtains the API token can control your agents.")
    os.environ["ORCHESTRATOR_BIND_PORT"] = str(port)  # the app accepts its own loopback origins
    _probe_instance_lock()
    if browser:
        _open_when_ready(host, port)
    # loop="asyncio" -> ProactorEventLoop on Windows (needed for subprocesses).
    uvicorn.run(
        "orchestrator.api.app:create_app", factory=True, host=host, port=port, loop="asyncio",
        log_level=log_level, reload=False, workers=1, access_log=access_log,
        # Never rewrite the client address from X-Forwarded-For / Forwarded: the worker-origin guard identifies
        # the caller from the real TCP peer (re-review N1).
        proxy_headers=False, forwarded_allow_ips="",
        log_config=uvicorn_log_config(),  # redacts `token=` query values in access/error logs (WebSocket URLs)
    )


# ---- dashboard / stop ------------------------------------------------------------------------------
@app.command()
def dashboard(
    ctx: typer.Context,
    port: Annotated[Optional[int], typer.Option(help="Port of the local server (default from policies.yaml, 8765).")] = None,
    no_browser: Annotated[bool, typer.Option("--no-browser", help="Start the server but do not open the browser.")] = False,
    json_out: JsonOpt = False,
    url: UrlOpt = None,
) -> None:
    """Start the backend in the background if needed, then open the dashboard in your browser.

    The background server logs to <data dir>/logs/server.log and records its pid in <data dir>/server.pid;
    stop it with `orchestrator stop`. The API token is only placed in the URL opened in the browser, never printed.

    \b

    Examples:
      orchestrator dashboard
      orchestrator dashboard --port 9000 --no-browser
    """
    with session(ctx, json_out, url) as s:
        if port is not None:
            s.api.base_url = f"http://127.0.0.1:{port}"
            s.api._client = None
        base = s.api.base_url
        port_n = int(base.rsplit(":", 1)[-1]) if base.rsplit(":", 1)[-1].isdigit() else int(_security().get("port", 8765))
        started, pid = False, None
        if is_running(base):
            rec = server.read_pid_file()
            pid = rec.pid if rec and server.is_our_server(rec)[0] else None
        else:
            if server.port_in_use(port_n):
                raise CliError(f"port {port_n} is in use by another program that is not the orchestrator; "
                               f"choose another port with `orchestrator dashboard --port {port_n + 1}`.")
            rec = server.read_pid_file()
            if rec is not None and not server.is_our_server(rec)[0]:
                server.remove_pid_file()  # stale
            s.out.note(f"starting the orchestrator in the background on port {port_n} ...")
            proc = server.spawn_server(port_n)
            pid, started = proc.pid, True
            server.write_pid_file(proc.pid, port_n)
            if not server.wait_until(lambda: proc.poll() is not None or is_running(base), timeout=60) or not is_running(base):
                tail = server.log_tail()
                exited = proc.poll() is not None
                if exited:
                    server.remove_pid_file()
                raise CliError(
                    ("the server exited during startup" if exited else "the server did not answer in 60 s")
                    + f". Log: {server.log_file()}" + (f"\n{tail}" if tail else "")
                )
        index_ok = (FRONTEND_DIST / "index.html").is_file()
        opened = False
        if not no_browser:
            try:
                opened = bool(webbrowser.open(f"{base}/#token={read_token()}"))
            except CliError:
                raise
            except Exception:  # noqa: BLE001
                opened = False
        info = {"url": base + "/", "started": started, "pid": pid, "browser_opened": opened,
                "log": str(server.log_file()), "frontend_built": index_ok}

        def render(out: Out, d: dict[str, Any]) -> None:
            out.line(("started" if d["started"] else "already running") + f": {d['url']}" + (f" (pid {d['pid']})" if d["pid"] else ""))
            if no_browser:
                out.line("browser not opened (--no-browser).")
            elif d["browser_opened"]:
                out.line("opened in your browser. The access token is in that browser URL only (it is not printed here).")
            else:
                out.line("could not open a browser; open the URL above. The dashboard asks for the token on first use: "
                         f"it is the content of {server_token_hint()}.")
            if not d["frontend_built"]:
                out.line("note: the dashboard UI is not built (frontend/dist missing); the API works. Build it with "
                         "`npm.cmd ci` and `npm.cmd run build` in frontend/, or run scripts\\start.cmd.")
            out.line("logs: " + d["log"])
            out.line("stop it with: orchestrator stop")

        s.show(info, render)


def server_token_hint() -> str:
    from orchestrator.cli.client import token_path

    return str(token_path())


@app.command()
def stop(ctx: typer.Context, json_out: JsonOpt = False, url: UrlOpt = None) -> None:
    """Stop the background server started by `orchestrator dashboard` (by the pid in server.pid only).

    The process command line is verified to be `orchestrator serve` before anything is terminated; processes
    are never killed by name. A foreground `orchestrator serve` is stopped with Ctrl+C in its own terminal.

    Example: orchestrator stop
    """
    with session(ctx, json_out, url) as s:
        rec = server.read_pid_file()
        if rec is None:
            s.show({"stopped": False, "reason": "no background server recorded"},
                   lambda o, d: o.line("no background server recorded (no server.pid); nothing to stop."))
            return
        ours, why = server.is_our_server(rec)
        if not ours:
            if "not running" in why or "stale" in why:
                server.remove_pid_file()
                s.show({"stopped": False, "reason": why, "pid": rec.pid},
                       lambda o, d: o.line(f"server.pid pointed at pid {rec.pid} but {why}; removed the stale pid file."))
                return
            raise CliError(f"{why}; refusing to terminate it. Remove {server.pid_file()} by hand if it is stale.")
        if not server.stop_pid(rec.pid):
            raise CliError(f"could not terminate pid {rec.pid} completely; close it from Task Manager.")
        server.remove_pid_file()
        s.show({"stopped": True, "pid": rec.pid, "port": rec.port},
               lambda o, d: o.line(f"stopped the background server (pid {rec.pid}" + (f", port {rec.port}" if rec.port else "") + ")."))


# ---- codex-attest ----------------------------------------------------------------------------------
@app.command("codex-attest")
def codex_attest(ctx: typer.Context, json_out: JsonOpt = False) -> None:
    """Attest the owned Codex runtime: run the native containment checks against the installed Codex.

    Model-free (no prompt is sent; only `codex --version`, `codex sandbox`, `codex mcp list` and
    `codex debug prompt-input` run, with an owned CODEX_HOME and synthetic auth). Writes
    <data dir>/codex-activation.json only when every check passes; Codex execution stays refused
    otherwise. Re-run after a Codex update. Exit code 0 only when the record was written.

    Example: orchestrator codex-attest
    """
    import asyncio

    from orchestrator.cli.common import global_opts
    from orchestrator.config import get_paths, real_providers_disabled
    from orchestrator.security import codex_activation

    out = Out()
    as_json = bool(json_out or global_opts(ctx).get("json"))
    if real_providers_disabled():
        out.error("ORCHESTRATOR_DISABLE_REAL_PROVIDERS=1: attestation needs the real Codex CLI; nothing was run.")
        raise typer.Exit(code=1)
    result = asyncio.run(codex_activation.attest(get_paths().data_dir))
    if as_json:
        out.json(result.as_dict())
    else:
        out.table(["check", "result", "detail"], [[c.name, c.result, c.detail] for c in result.checks])
        out.kv([("codex version", result.codex_version), ("executable sha256", result.executable_sha256),
                ("code identity", result.code_identity_sha256), ("scratch cleanup", result.cleanup)])
        if result.error:
            out.line(f"error: {result.error}")
        out.line(f"recorded: {result.record_path}" if result.recorded
                 else "NOT recorded: Codex execution remains refused.")
    if not result.recorded:
        raise typer.Exit(code=1)


# ---- client commands -------------------------------------------------------------------------------
app.command("status")(cmd_info.status)
app.add_typer(cmd_info.providers_app, name="providers")
app.command("models")(cmd_info.models)
app.command("usage")(cmd_info.usage)
app.add_typer(cmd_work.projects_app, name="projects")
app.add_typer(cmd_work.tasks_app, name="tasks")
app.command("run")(cmd_work.run)
app.command("resume")(cmd_work.resume)
app.command("pause")(cmd_work.pause)
app.command("cancel")(cmd_work.cancel)
app.command("logs")(cmd_work.logs)
app.add_typer(cmd_work.approvals_app, name="approvals")
app.add_typer(cmd_info.skills_app, name="skills")
app.add_typer(cmd_info.routing_app, name="routing")
app.add_typer(cmd_info.leader_app, name="leader")
app.add_typer(cmd_info.advisor_app, name="advisor")
app.add_typer(cmd_info.knowledge_app, name="knowledge")
app.add_typer(cmd_info.analytics_app, name="analytics")
app.command("doctor")(doctor_mod.doctor)

if __name__ == "__main__":
    app()
