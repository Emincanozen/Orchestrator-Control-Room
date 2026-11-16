"""Paths, YAML config loading and effective policies.

Effective policies = config/policies.yaml defaults deep-merged with overrides
stored in the `settings` table (one row per top-level section, e.g. key
"limits" -> {"max_parallel_workers": 2}).
"""

from __future__ import annotations

import copy
import logging
import os
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml

PACKAGE_DIR = Path(__file__).resolve().parent
BACKEND_DIR = PACKAGE_DIR.parent
REPO_ROOT = BACKEND_DIR.parent
CONFIG_DIR = Path(os.environ.get("ORCHESTRATOR_CONFIG_DIR", REPO_ROOT / "config"))
FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"

ENV_DATA_DIR = "ORCHESTRATOR_DATA_DIR"
ENV_WORKTREES_DIR = "ORCHESTRATOR_WORKTREES_DIR"
ENV_ENABLE_MOCK = "ORCHESTRATOR_ENABLE_MOCK"
ENV_DISABLE_REAL = "ORCHESTRATOR_DISABLE_REAL_PROVIDERS"
DEPTH_ENV = "AI_ORCHESTRATOR_DEPTH"

APP_DIR_WINDOWS = "AI-Orchestrator"
APP_DIR_POSIX = "ai-orchestrator"
# Data location before security finding #1/#8: inside the checkout. Only `orchestrator doctor` looks at it.
LEGACY_DATA_DIR = REPO_ROOT / "data"

log = logging.getLogger(__name__)


def install_dirs() -> list[Path]:
    """The orchestrator's own installation (re-review N3): the checkout, the configuration directory (also when
    ORCHESTRATOR_CONFIG_DIR points elsewhere), the Python environment it runs from (venv and base interpreter),
    the package and the built dashboard. An agent that can write here can change the policies
    (`claude_shell`, `auto_approve`) or the server's code, so no project and no run may be, contain or be
    inside one of them."""
    out: list[Path] = [REPO_ROOT, CONFIG_DIR, PACKAGE_DIR, FRONTEND_DIST, Path(sys.prefix), Path(sys.base_prefix)]
    env_cfg = os.environ.get("ORCHESTRATOR_CONFIG_DIR")
    if env_cfg:
        out.append(Path(env_cfg))
    seen: dict[str, Path] = {}
    for p in out:
        try:
            real = Path(os.path.realpath(p))
        except (OSError, ValueError):
            continue
        seen.setdefault(os.path.normcase(str(real)), real)
    return list(seen.values())


class PathsConfigError(ValueError):
    """The data / worktree directory configuration is unsafe (e.g. worktrees inside the data dir)."""


def default_data_dir() -> Path:
    """%LOCALAPPDATA%/AI-Orchestrator on Windows; $XDG_DATA_HOME (or ~/.local/share)/ai-orchestrator elsewhere."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / APP_DIR_WINDOWS
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / APP_DIR_POSIX


def default_worktrees_dir(data_dir: Path) -> Path:
    """Sibling of the data dir (`<parent>/<name>-worktrees`): agent worktrees never sit next to the token or DB."""
    return data_dir.parent / f"{data_dir.name}-worktrees"


def _nested(a: Path, b: Path) -> bool:
    """True when a == b or one contains the other (case-insensitive on Windows)."""
    na, nb = (os.path.normcase(os.path.normpath(str(x))) for x in (a, b))
    if na == nb:
        return True
    return na.startswith(nb.rstrip("\\/") + os.sep) or nb.startswith(na.rstrip("\\/") + os.sep)


@dataclass(frozen=True)
class Paths:
    data_dir: Path
    worktrees: Optional[Path] = None  # None = default_worktrees_dir(data_dir)

    def __post_init__(self) -> None:
        if _nested(self.data_dir, self.worktrees_dir):
            raise PathsConfigError(
                f"unsafe configuration: the worktrees dir ({self.worktrees_dir}) and the data dir ({self.data_dir}) "
                "must not be the same or contain each other; agents work in worktrees and must not reach the "
                f"API token or the database. Set {ENV_WORKTREES_DIR} or {ENV_DATA_DIR} to separate folders."
            )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "orchestrator.db"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "api-token"

    @property
    def worktrees_dir(self) -> Path:
        return self.worktrees if self.worktrees is not None else default_worktrees_dir(self.data_dir)

    @property
    def leader_scratch_dir(self) -> Path:
        """cwd for leader/advisor/switch runs that have no project directory. Outside the data dir on purpose."""
        return self.worktrees_dir / "_scratch" / "leader"

    @property
    def sandbox_dir(self) -> Path:
        """Writable area of sandboxed verification commands (Low integrity label; finding N2): per project a
        `cache/` (npm, uv, pip, XDG caches) and per command `tmp/<id>/` (TEMP). Outside the data dir on purpose."""
        return self.worktrees_dir / "_sandbox"

    def sandbox_root(self, project_id: Optional[str]) -> Path:
        """The sandbox dir of one project (caches are never shared between projects)."""
        import hashlib
        import re

        pid = project_id or "_adhoc"
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,80}", pid):
            pid = "p-" + hashlib.sha256(pid.encode("utf-8")).hexdigest()[:24]
        return self.sandbox_dir / pid

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def skills_dir(self) -> Path:
        return self.data_dir / "skills"

    @property
    def tmp_dir(self) -> Path:
        """Owner-only scratch files of the orchestrator itself (Codex output schemas, verification logs), instead
        of %TEMP%, which sandboxed Codex workers can write (re-review N8). Emptied at every server start."""
        return self.data_dir / "tmp"

    def clean_tmp(self) -> None:
        """Remove leftovers of a previous server run (call with the instance lock held). Never raises."""
        import shutil

        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @property
    def protected_dirs(self) -> list[Path]:
        """Directories no provider run may use as cwd or extra dir: the data dir (token, DB) and the
        orchestrator's own installation (code, config, venv). Runs happen inside worktrees, so the worktrees
        dir is only protected against project registration (`registration_protected_dirs`)."""
        return [self.data_dir, *install_dirs()]

    @property
    def registration_protected_dirs(self) -> list[Path]:
        """Directories a registered project may not be, contain or be inside."""
        return [self.data_dir, self.worktrees_dir, *install_dirs()]

    def ensure(self, *, harden: Optional[bool] = None) -> "Paths":
        """Create the directories and make data dir and worktrees dir owner-only (security finding #8).

        `harden=None` follows ORCHESTRATOR_HARDEN_ACL (default on). A hardening failure is logged, not raised."""
        for p in (self.data_dir, self.worktrees_dir, self.logs_dir, self.tmp_dir):
            p.mkdir(parents=True, exist_ok=True)
        from orchestrator.security import acl, sandbox

        if harden if harden is not None else acl.hardening_enabled():
            for d in (self.data_dir, self.worktrees_dir):
                if acl.check_private(d):
                    acl.harden_private_dir(d)
        # Not tied to the ACL switch above: the integrity label is what keeps sandboxed verification code (Low
        # integrity, same user) from reading or writing the database and the token (finding N2). Inherited by
        # every file created later (WAL/SHM, logs, tmp). Medium processes are unaffected.
        if sandbox.supported() and sandbox.check_data_dir_label(self.data_dir):
            sandbox.protect_data_dir(self.data_dir)
        # Everything under the worktrees dir is a task worktree for orchestrator git: its git dir comes from the
        # project repository, never from a `.git` file an agent or a sandboxed command can rewrite (N2 S2).
        from orchestrator.gitops import git

        git.register_worktree_root(self.worktrees_dir)
        return self


def get_paths(data_dir: str | Path | None = None, worktrees_dir: str | Path | None = None) -> Paths:
    """Effective paths. An explicit `data_dir` (tests, embedding) ignores the environment entirely, so the
    worktrees dir is then `worktrees_dir` or the data dir's sibling; otherwise ORCHESTRATOR_DATA_DIR /
    ORCHESTRATOR_WORKTREES_DIR apply. Raises PathsConfigError for nested data/worktrees dirs."""
    if data_dir:
        raw, wt = data_dir, worktrees_dir
    else:
        raw = os.environ.get(ENV_DATA_DIR) or default_data_dir()
        wt = worktrees_dir or os.environ.get(ENV_WORKTREES_DIR) or None
    return Paths(Path(raw).resolve(), Path(wt).resolve() if wt else None)


def mock_enabled() -> bool:
    return os.environ.get(ENV_ENABLE_MOCK, "") == "1"


def real_providers_disabled() -> bool:
    """True when the real claude/codex/agy adapters must not be built (tests set this to 1)."""
    return os.environ.get(ENV_DISABLE_REAL, "") == "1"


def load_yaml(name: str, config_dir: Path | None = None) -> dict[str, Any]:
    path = (config_dir or CONFIG_DIR) / name
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping at top level")
    return data


@lru_cache(maxsize=8)
def _cached_policies(config_dir: str) -> dict[str, Any]:
    return load_yaml("policies.yaml", Path(config_dir))


def default_policies(config_dir: Path | None = None) -> dict[str, Any]:
    return copy.deepcopy(_cached_policies(str(config_dir or CONFIG_DIR)))


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


# Hard fallbacks used when a key is missing from both YAML and settings.
_LIMIT_FALLBACKS: dict[str, float] = {
    "max_parallel_workers": 3,
    "max_parallel_per_project": 2,
    "max_retry_attempts": 3,
    "max_repair_cycles": 3,
    "max_consecutive_failures": 3,
    "max_active_runtime_seconds": 3600,
    "run_heartbeat_timeout_seconds": 300,
    "max_consultations_per_task": 2,
    "max_leader_calls_per_task": 6,
    "quota_recheck_seconds": 300,
    "verify_timeout_seconds": 900,
    "retry_backoff_seconds": 5,
}


class Policies:
    """Read-only view over an effective policy dict."""

    def __init__(self, data: dict[str, Any]):
        self.data = data

    def limit(self, key: str) -> float:
        limits = self.data.get("limits") or {}
        value = limits.get(key, _LIMIT_FALLBACKS.get(key))
        if value is None:
            raise KeyError(f"unknown limit {key}")
        return value

    def int_limit(self, key: str) -> int:
        return int(self.limit(key))

    @property
    def approvals_required(self) -> set[str]:
        return set((self.data.get("approvals") or {}).get("required_for") or [])

    @property
    def auto_approve(self) -> set[str]:
        return set((self.data.get("approvals") or {}).get("auto_approve") or [])

    @property
    def security(self) -> dict[str, Any]:
        return self.data.get("security") or {}

    @property
    def child_env_allowlist(self) -> list[str]:
        return list(self.security.get("child_env_allowlist") or [])

    @property
    def review_required(self) -> bool:
        return bool((self.data.get("review") or {}).get("required", False))

    def worker_permission(self, phase: str) -> str:
        wp = self.data.get("worker_permissions") or {}
        if phase in ("plan", "review", "advise"):
            return str(wp.get("plan_review_advise", "read_only"))
        return str(wp.get("default", "workspace_write"))

    @property
    def claude_shell(self) -> bool:
        """YAML-only opt-in: Claude workspace_write runs also get Bash (NOT confined to the worktree)."""
        return (self.data.get("worker_permissions") or {}).get("claude_shell") is True


# (section, key) pairs that only config/policies.yaml may set. Overrides from the settings table (i.e. the API)
# are dropped: an agent that obtained the API token must not be able to widen its own permissions
# (security finding #1). The settings DTO may still accept the section; the value is ignored here.
YAML_ONLY_KEYS: frozenset[tuple[str, str]] = frozenset({("worker_permissions", "claude_shell")})


def _strip_yaml_only(overrides: dict[str, Any]) -> dict[str, Any]:
    out = dict(overrides)
    for section, key in YAML_ONLY_KEYS:
        sec = out.get(section)
        if isinstance(sec, dict) and key in sec:
            log.warning("ignoring settings override %s.%s: it can only be set in config/policies.yaml", section, key)
            out[section] = {k: v for k, v in sec.items() if k != key}
    return out


def effective_policies(overrides: dict[str, Any], config_dir: Path | None = None) -> Policies:
    return Policies(deep_merge(default_policies(config_dir), _strip_yaml_only(overrides)))


def yaml_claude_shell(config_dir: Path | None = None) -> bool:
    """`worker_permissions.claude_shell` from config/policies.yaml only (what the Claude adapter uses)."""
    return Policies(default_policies(config_dir)).claude_shell


# Never passed to a child process even when allowlisted: they point at the data dir / API token (finding #1).
NEVER_INHERITED_PREFIXES = ("ORCHESTRATOR_",)


def never_inherited(name: str) -> bool:
    return name.upper().startswith(NEVER_INHERITED_PREFIXES)


def child_env(allowlist: list[str], extra: dict[str, str] | None = None) -> dict[str, str]:
    """Allowlisted environment for child processes (case-insensitive on Windows)."""
    wanted = {k.upper() for k in allowlist}
    env = {k: v for k, v in os.environ.items() if k.upper() in wanted and not never_inherited(k)}
    depth = int(os.environ.get(DEPTH_ENV, "0") or 0)
    env[DEPTH_ENV] = str(depth + 1)
    if extra:
        env.update(extra)
    return env
