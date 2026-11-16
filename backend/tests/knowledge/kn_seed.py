"""Seeding helpers: real rows inserted with explicit timestamps (no mocks of the DB layer)."""

from __future__ import annotations

import json
from typing import Any, Optional

from orchestrator.config import get_paths
from orchestrator.services.context import AppContext

_n = {"i": 0}


def open_ctx(tmp_path) -> AppContext:
    return AppContext.open(get_paths(tmp_path / "data"))


def uid(prefix: str) -> str:
    _n["i"] += 1
    return f"{prefix}_{_n['i']:06d}"


def project(ctx: AppContext, path: str, pid: Optional[str] = None) -> str:
    pid = pid or uid("prj")
    ctx.db.write_sync(lambda tx: tx.execute(
        "INSERT INTO projects (id, name, path, created_at, updated_at) VALUES (?,?,?,?,?)", (pid, pid, path, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00")))
    return pid


def task(ctx: AppContext, pid: str, *, state: str = "COMPLETED", task_type: str = "coding", title: str = "t", finished: Optional[str] = None,
         retries: int = 0, repairs: int = 0, active: float = 0, tid: Optional[str] = None, **extra: Any) -> str:
    tid = tid or uid("tsk")
    finished = finished or "2026-03-01T10:00:00+00:00"
    cols = {"id": tid, "project_id": pid, "title": title, "task_type": task_type, "state": state, "retry_count": retries,
            "repair_cycles": repairs, "active_seconds": active, "created_at": "2026-03-01T09:00:00+00:00", "updated_at": finished,
            "finished_at": finished, **extra}
    ctx.db.write_sync(lambda tx: tx.execute(f"INSERT INTO tasks ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", list(cols.values())))
    return tid


def run(ctx: AppContext, tid: Optional[str], *, provider: str = "claude", model: Optional[str] = "m1", phase: str = "execute", status: str = "succeeded",
        start: str = "2026-03-01T09:10:00+00:00", end: Optional[str] = "2026-03-01T09:11:00+00:00", usage: Optional[dict] = None,
        category: Optional[str] = None, message: Optional[str] = None, agent_id: Optional[str] = None) -> str:
    rid = uid("run")
    ctx.db.write_sync(lambda tx: tx.execute(
        "INSERT INTO runs (id, task_id, phase, agent_id, provider, model, status, error_category, error_message, usage, started_at, finished_at, idempotency_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, tid, phase, agent_id, provider, model, status, category, message, json.dumps(usage or {}), start, end, rid)))
    return rid


def verification(ctx: AppContext, tid: str, status: str, start: str, checks: Optional[list] = None) -> str:
    vid = uid("vrf")
    ctx.db.write_sync(lambda tx: tx.execute(
        "INSERT INTO verifications (id, task_id, status, checks, started_at, finished_at) VALUES (?,?,?,?,?,?)",
        (vid, tid, status, json.dumps(checks or []), start, start)))
    return vid


def sql(ctx: AppContext, statement: str, params: tuple = ()) -> None:
    ctx.db.write_sync(lambda tx: tx.execute(statement, params))


# ---- git helpers (real repositories on disk) ------------------------------------------------------
import os  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(cwd: Path, *args: str) -> str:
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env=_ENV)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def write(root: Path, files: dict) -> None:
    for rel, content in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))


def make_repo(root: Path, files: dict, message: str = "init") -> str:
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "core.autocrlf", "false")
    write(root, files)
    git(root, "add", "-A")
    git(root, "commit", "-q", "--allow-empty", "-m", message)
    return git(root, "rev-parse", "HEAD")
