"""Helpers for the skills tests: local fake skill repositories, an injected source policy, fake catalogs.

No network: every "remote" is a temp git repository on disk; the allowed-source policy is injected."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Optional

import yaml

from orchestrator.skills.errors import SourceNotAllowed
from orchestrator.skills.manager import SkillManager
from orchestrator.skills.sources import SourcePolicy, SourceSpec, check_subpath

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


def git(cwd: Path, *args: str, input_text: Optional[str] = None) -> str:
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env=_ENV, input=input_text)
    assert p.returncode == 0, f"git {args} failed: {p.stderr}"
    return p.stdout.strip()


def make_repo(path: Path, files: dict[str, str | bytes], *, symlinks: Optional[dict[str, str]] = None, message: str = "init") -> str:
    """Create (or extend) a git repo at `path` with a commit of `files`; returns the commit sha."""
    path.mkdir(parents=True, exist_ok=True)
    if not (path / ".git").exists():
        git(path, "init", "-q", "-b", "main")
        git(path, "config", "core.autocrlf", "false")
    for rel, content in files.items():
        f = path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    git(path, "add", "-A")
    for rel, target in (symlinks or {}).items():  # real symlink entries (mode 120000) without needing OS symlink rights
        blob = git(path, "hash-object", "-w", "--stdin", input_text=target)
        git(path, "update-index", "--add", "--cacheinfo", f"120000,{blob},{rel}")
    git(path, "commit", "-q", "--allow-empty", "-m", message)
    return git(path, "rev-parse", "HEAD")


class LocalPolicy(SourcePolicy):
    """`local:<name>[::sub/path]` -> a repository on disk. Anything else is refused (like a non-GitHub URL)."""

    def __init__(self, repos: dict[str, Path]):
        self.repos = repos

    def parse(self, url: str) -> SourceSpec:
        if not url.startswith("local:"):
            raise SourceNotAllowed("only local test sources are allowed here")
        name, _, sub = url[len("local:"):].partition("::")
        if name not in self.repos:
            raise SourceNotAllowed(f"unknown local source {name}")
        return SourceSpec(clone_url=str(self.repos[name]), subpath=check_subpath(sub), local=True)


def entry(id: str, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": id, "user_label": id, "name": id, "kind": "agent_skill", "category": "test", "source_url": f"local:{id}",
        "source_verified": True, "uncertainty": None, "candidates": [], "description": f"test skill {id}", "license": "MIT",
        "maintenance": {}, "default_branch": "main", "pinned_ref": None, "compat": {"claude_code": "yes", "codex": "unknown", "mcp": "n/a"},
        "install_method": {"type": "manual", "command": None, "notes": "n"}, "dependencies": [], "permissions": [],
        "requires_credentials": False, "paid_service": "no", "security": {"risk": "low", "notes": ""}, "priority_candidate": False,
        "initial_state": "CATALOGUED_ON_DEMAND", "duplicates_of": None,
    }
    base.update(kw)
    return base


def write_catalog(path: Path, entries: list[dict[str, Any]]) -> Path:
    path.write_text(yaml.safe_dump({"catalog_version": 1, "researched_at": "2026-11-11", "skills": entries}, sort_keys=False), encoding="utf-8")
    return path


SKILL_MD = "---\nname: {name}\ndescription: A test skill called {name}\n---\n\n# {name}\n\nDo the thing.\n"


def simple_skill_files(name: str) -> dict[str, str]:
    return {"SKILL.md": SKILL_MD.format(name=name), "notes/extra.md": "extra\n"}


async def make_manager(h, tmp_path: Path, repos: dict[str, Path], **kw: Any) -> SkillManager:
    kw.setdefault("which", lambda name: f"/fake/{name}")
    return SkillManager(h.ctx, h.approvals, source_policy=LocalPolicy(repos), **kw)


async def approve_pending(h, approval_id: str) -> None:
    await h.approvals.resolve(approval_id, True, by="user")
