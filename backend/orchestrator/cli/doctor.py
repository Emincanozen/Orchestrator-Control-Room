"""`orchestrator doctor`: local environment checks. Never calls a model, never spends quota."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

import typer

from orchestrator.cli import server
from orchestrator.cli.client import is_running, token_path
from orchestrator.cli.common import JsonOpt, UrlOpt, session
from orchestrator.cli.output import Out
from orchestrator.config import LEGACY_DATA_DIR, FRONTEND_DIST, PathsConfigError, get_paths, real_providers_disabled

OK, WARN, FAIL = "ok", "warn", "FAIL"


def _check(name: str, status: str, detail: str, fix: str = "") -> dict[str, str]:
    return {"check": name, "status": status, "detail": detail, "fix": fix}


def tool_version(exe: str, *args: str) -> Optional[str]:
    path = shutil.which(exe)
    if not path:
        return None
    try:
        r = subprocess.run([path, *args], capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr).strip() else "unknown"


def detect_providers() -> dict[str, Any]:
    """Provider detection via the registry (version / auth probes only; no model call). Injectable in tests."""
    from orchestrator.providers.registry import ProviderRegistry

    registry = ProviderRegistry.build_default()
    return asyncio.run(registry.refresh())


def legacy_data_check(legacy: Path = LEGACY_DATA_DIR) -> Optional[dict[str, str]]:
    """Warn when a pre-#1 data dir (`<repo>/data/orchestrator.db`) still exists; it is no longer used."""
    db = legacy / "orchestrator.db"
    if not db.is_file():
        return None
    return _check("legacy data dir", WARN, f"{db} exists but is no longer used (data now lives in {get_paths().data_dir})",
                  f"Move what you need, then delete {legacy}: inside the checkout its token and DB are readable "
                  "by agents working on this repository.")


def acl_checks() -> list[dict[str, str]]:
    """Owner-only permissions on the data dir, the token file and the worktrees dir (security finding #8).
    Only other local accounts are covered; processes of the same user are not restricted by ACLs."""
    from orchestrator.security import acl

    try:
        paths = get_paths()
    except PathsConfigError as exc:
        return [_check("data / worktrees dirs", FAIL, str(exc), "Point ORCHESTRATOR_DATA_DIR and "
                       "ORCHESTRATOR_WORKTREES_DIR at two separate folders.")]
    out: list[dict[str, str]] = []
    for label, path in (("data dir permissions", paths.data_dir), ("api token permissions", paths.token_path),
                        ("worktrees dir permissions", paths.worktrees_dir)):
        if not path.exists():
            continue
        problems = acl.check_private(path)
        if problems:
            if path == paths.token_path:
                advice = ("Restart the backend: it re-applies owner-only permissions to the token file, or replaces the "
                          "token when the file belongs to another account or cannot be restricted (then reopen the "
                          "dashboard with `orchestrator dashboard`). The server log has a 'SECURITY:' line either way.")
            else:
                advice = ("Restart the backend: at startup it makes this directory owner-only. If the problem stays, "
                          "the server log has a 'SECURITY:' line explaining why that failed.")
            if not acl.hardening_enabled():
                advice = f"ORCHESTRATOR_HARDEN_ACL=0 switches the hardening off; unset it. {advice}"
            out.append(_check(label, WARN, f"{path}: {'; '.join(problems)}", advice))
        else:
            out.append(_check(label, OK, f"{path}: owner-only"))
    return out


def sandbox_checks() -> list[dict[str, str]]:
    """Verification sandbox (finding N2): a Low-integrity token can be created here, and the data dir carries the
    Medium no-read-up/no-write-up label that keeps sandboxed code away from the database and the token."""
    from orchestrator.security import sandbox

    if not sandbox.supported():
        return [_check("verification sandbox", FAIL, "not implemented on this platform: verification commands are "
                       "refused (fail closed)", "Run the orchestrator on Windows to use command verification.")]
    out: list[dict[str, str]] = []
    try:
        elevated = sandbox.is_elevated()
    except sandbox.SandboxUnavailable as exc:
        return [_check("not elevated", FAIL, str(exc), "Retry from a normal terminal with token query access.")]
    out.append(_check("not elevated", FAIL if elevated else OK,
                      "this process runs as administrator: the server refuses to start elevated" if elevated
                      else "running without administrator rights",
                      "Start the orchestrator from a normal (non-administrator) terminal." if elevated else ""))
    reason = sandbox.self_test()
    out.append(_check("verification sandbox", OK if reason is None else FAIL,
                      "Low-integrity token available" if reason is None else f"cannot create a Low-integrity token: {reason}",
                      "" if reason is None else "Verification commands will be refused until this works."))
    try:
        d = get_paths().data_dir
    except PathsConfigError:
        return out
    if d.exists():
        problems = sandbox.check_data_dir_label(d)
        out.append(_check("data dir integrity label", WARN if problems else OK,
                          "; ".join(problems) if problems else f"{d}: Medium, no-read-up / no-write-up (inherited)",
                          "Restart the backend: at startup it applies the label. If it stays, the server log has a "
                          "'SECURITY:' line." if problems else ""))
    return out


def data_dir_check() -> dict[str, str]:
    try:
        d = get_paths().data_dir
    except PathsConfigError as exc:
        return _check("data dir writable", FAIL, str(exc), "Use separate folders for data and worktrees.")
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / f".doctor-{uuid.uuid4().hex[:8]}"
        probe.write_text("x", encoding="ascii")
        probe.unlink()
    except OSError as exc:
        return _check("data dir writable", FAIL, f"{d}: {exc.__class__.__name__}",
                      "Pick a writable folder: set ORCHESTRATOR_DATA_DIR (see docs/INSTALLATION.md).")
    return _check("data dir writable", OK, str(d))


def codex_activation_check(statuses: dict[str, Any] | None) -> dict[str, str]:
    """Owned Codex runtime attestation. Reads the record only; the binary match comes from provider detection
    (`statuses`, None when detection was skipped). Never runs the attestation itself."""
    from orchestrator.security import codex_activation

    name, fix = "codex activation", "Run `orchestrator codex-attest` (model-free native checks; re-run after a Codex update)."
    try:
        summary = codex_activation.status_summary(get_paths().data_dir)
    except PathsConfigError as exc:
        return _check(name, WARN, str(exc), fix)
    if not summary["record"]:
        return _check(name, WARN, f"Codex execution refused: {summary['reason']}", fix)
    attested = f"attested {summary['codex_version']} at {summary['attested_at']}"
    if not summary["code_matches"]:
        return _check(name, WARN, f"{attested}; stale: isolation source changed since attestation", fix)
    codex = (statuses or {}).get("codex")
    error = getattr(codex, "error", None) or ""
    if statuses is None:
        return _check(name, OK, f"{attested} (installed binary not compared: provider detection skipped)")
    if codex is None or not getattr(codex, "installed", False):
        return _check(name, WARN, f"{attested}; Codex CLI not detected", "Optional. Install the Codex CLI.")
    if codex_activation.REFUSAL_PREFIX in error:
        return _check(name, WARN, error, fix)
    return _check(name, OK, f"{attested}; matches the installed binary")


def run_checks(base_url: str) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    v = sys.version_info
    checks.append(_check("python", OK if v >= (3, 12) else FAIL, f"{v.major}.{v.minor}.{v.micro} ({sys.executable})",
                         "" if v >= (3, 12) else "Python 3.12 is required: run the CLI through uv (`uv sync` in backend/)."))

    node = tool_version("node", "--version")
    if node is None:
        checks.append(_check("node", FAIL, "not found on PATH", "Install Node.js 22 LTS from https://nodejs.org and reopen the terminal."))
    else:
        major = int(node.lstrip("v").split(".")[0]) if node.lstrip("v")[:1].isdigit() else 0
        checks.append(_check("node", OK if major >= 22 else WARN, node, "" if major >= 22 else "Node 22 is the tested version; upgrade if the frontend build fails."))

    git = tool_version("git", "--version")
    checks.append(_check("git", OK if git else FAIL, git or "not found on PATH",
                         "" if git else "Install Git for Windows from https://git-scm.com and reopen the terminal."))
    uv = tool_version("uv", "--version")
    checks.append(_check("uv", OK if uv else WARN, uv or "not found on PATH (only needed to launch via scripts\\orchestrator.cmd)",
                         "" if uv else "Install uv: https://docs.astral.sh/uv/getting-started/installation/"))

    checks.append(data_dir_check())
    legacy = legacy_data_check()
    if legacy:
        checks.append(legacy)
    checks.extend(acl_checks())
    checks.extend(sandbox_checks())
    tp = token_path()
    checks.append(_check("api token file", OK if tp.is_file() else WARN, str(tp) if tp.is_file() else f"{tp} does not exist yet",
                         "" if tp.is_file() else "It is created on the first start: run `orchestrator dashboard`."))

    port = urlsplit(base_url).port or 80
    if is_running(base_url):
        checks.append(_check("port / server", OK, f"orchestrator is answering at {base_url}"))
    elif server.port_in_use(port):
        checks.append(_check("port / server", FAIL, f"port {port} is in use by something that is not the orchestrator",
                             f"Free the port, or use another one: `orchestrator dashboard --port {port + 1}`."))
    else:
        checks.append(_check("port / server", OK, f"port {port} is free; the backend is not running"))

    statuses: dict[str, Any] = {}
    if real_providers_disabled():
        checks.append(_check("provider CLIs", WARN, "skipped: ORCHESTRATOR_DISABLE_REAL_PROVIDERS=1"))
    else:
        try:
            statuses = detect_providers()
        except Exception as exc:  # noqa: BLE001
            checks.append(_check("provider CLIs", WARN, f"detection failed: {exc.__class__.__name__}"))
            statuses = {}
        for name, st in sorted(statuses.items()):
            if not st.installed:
                checks.append(_check(f"provider {name}", WARN, st.error or "CLI not found",
                                     f"Optional. Install the `{name}` CLI and log in to use it as a provider."))
            elif st.authenticated is False:
                checks.append(_check(f"provider {name}", WARN, f"{st.version or 'installed'}; not logged in",
                                     f"Run the `{name}` CLI once and sign in."))
            else:
                auth = "logged in" if st.authenticated else "login state unknown"
                checks.append(_check(f"provider {name}", OK, f"{st.version or 'installed'}; {auth}"))

    checks.append(codex_activation_check(statuses if not real_providers_disabled() else None))

    index = FRONTEND_DIST / "index.html"
    checks.append(_check("frontend build", OK if index.is_file() else WARN, str(index) if index.is_file() else f"{index} is missing",
                         "" if index.is_file() else "Build it: `cd frontend` then `npm.cmd ci` and `npm.cmd run build` (or run scripts\\start.cmd)."))
    return checks


def render(out: Out, checks: list[dict[str, str]]) -> None:
    out.table(["check", "status", "detail"], [[c["check"], c["status"], c["detail"]] for c in checks])
    fixes = [c for c in checks if c["fix"] and c["status"] != OK]
    if fixes:
        out.line("")
        out.line("how to fix:")
        for c in fixes:
            out.line(f"  - {c['check']}: {c['fix']}")


def doctor(ctx: typer.Context, json_out: JsonOpt = False, url: UrlOpt = None) -> None:
    """Check this machine: Python / Node / git, data dir, token, port, provider CLIs, frontend build.

    Local only: it does not call any model and spends no quota. Exit code 1 if a required check fails.

    Example: orchestrator doctor
    """
    with session(ctx, json_out, url) as s:
        checks = run_checks(s.api.base_url)
        s.show(checks, lambda o, d: render(o, d))
        if any(c["status"] == FAIL for c in checks):
            raise typer.Exit(code=1)
