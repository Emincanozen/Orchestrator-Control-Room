"""Writing a skill into a project and removing exactly what was written.

Project scope only: `<project>/.claude/skills/<name>/` (agent skills) and `<project>/.mcp.json` (MCP servers).
Nothing here touches the user's global ~/.claude, ~/.codex or ~/.agents. All paths are validated with
security.paths (resolve_inside) against the registered project root. Nothing is executed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Optional

from orchestrator.security.paths import PathPolicyError, resolve_inside
from orchestrator.skills.errors import ActivationRefused
from orchestrator.util import now_iso

log = logging.getLogger(__name__)

MARKER = ".orchestrator-skill.json"
TARGET_DIRS = {"claude": ".claude/skills", "codex": ".agents/skills"}
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def safe_name(name: str) -> str:
    n = re.sub(r"[^a-z0-9._-]+", "-", (name or "").strip().lower()).strip("-.")
    if not _NAME_RE.match(n):
        raise ActivationRefused(f"cannot derive a safe folder name from {name!r}")
    return n


def _inside(project_root: str, target: Path) -> Path:
    try:
        return resolve_inside(str(target), [project_root])
    except PathPolicyError as exc:
        raise ActivationRefused(f"path policy: {exc}") from exc


def _read_marker(folder: Path) -> Optional[dict[str, Any]]:
    try:
        return json.loads((folder / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _first_missing_dirs(path: Path) -> list[Path]:
    """Directories that do not exist yet on the way to `path` (top-down)."""
    missing = []
    p = path
    while not p.exists():
        missing.append(p)
        if p.parent == p:
            break
        p = p.parent
    return list(reversed(missing))


def _rel_files(src: Path) -> list[str]:
    out = []
    for p in sorted(src.rglob("*")):
        if p.is_symlink():
            raise ActivationRefused(f"symlink in package: {p.relative_to(src).as_posix()}")
        if p.is_file():
            out.append(p.relative_to(src).as_posix())
    return out


def install_skill_dir(src: Path, project_root: str, target_key: str, folder_name: str, skill_id: str, ref: str) -> dict[str, Any]:
    """Copy `src` into `<project>/<TARGET_DIRS[target]>/<folder_name>/`. Refuses to touch a folder this manager did
    not create (no marker for this skill id). Returns the artifact record needed to remove it again."""
    base = TARGET_DIRS[target_key]
    target = _inside(project_root, Path(project_root) / base / safe_name(folder_name))
    root_real = Path(os.path.realpath(project_root))
    if target.exists():
        marker = _read_marker(target)
        if not marker or marker.get("skill_id") != skill_id:
            raise ActivationRefused(
                f"{target} already exists and was not created by the orchestrator for this skill; refusing to overwrite it",
                details={"path": str(target)},
            )
        remove_artifact({"type": "skill_dir", "path": str(target), "files": marker.get("files", []), "created_dirs": []})
    files = _rel_files(src)
    created = _first_missing_dirs(target)
    try:
        target.mkdir(parents=True, exist_ok=True)
        for rel in files:
            dest = target.joinpath(*rel.split("/"))
            if not Path(os.path.realpath(dest)).is_relative_to(Path(os.path.realpath(target))):
                raise ActivationRefused(f"path escapes the target folder: {rel}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes((src / rel).read_bytes())
        (target / MARKER).write_text(
            json.dumps({"managed_by": "ai-orchestrator", "skill_id": skill_id, "ref": ref, "files": files, "created_at": now_iso()}, indent=1),
            encoding="utf-8",
        )
    except BaseException:
        remove_artifact({"type": "skill_dir", "path": str(target), "files": files, "created_dirs": [str(c) for c in created]})
        raise
    return {
        "type": "skill_dir", "target": target_key, "path": str(target), "files": files,
        "created_dirs": [str(c) for c in created if Path(os.path.realpath(c)).is_relative_to(root_real)],
    }


def _rmdir_if_empty(p: Path) -> bool:
    try:
        p.rmdir()
        return True
    except OSError:
        return False


def confined_file(target: Path, rel: Any) -> Optional[Path]:
    """The path of `rel` inside `target`, or None when the entry is not a plain relative path that stays inside
    the skill's install directory. Used on lists that came from disk (the marker file is untrusted): rejects
    non-strings, absolute and drive-relative paths, `..`/`.`/empty parts, backslash/colon tricks, and any entry
    whose parent directory resolves (junction/symlink) outside `target`."""
    if not isinstance(rel, str) or not rel or chr(0) in rel or chr(92) in rel or ":" in rel or rel.startswith("/"):
        return None
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    f = target.joinpath(*parts)
    try:
        real_target = Path(os.path.realpath(target))
        if not Path(os.path.realpath(f.parent)).is_relative_to(real_target):
            return None
    except (OSError, ValueError):
        return None
    return f


def remove_artifact(art: dict[str, Any]) -> dict[str, Any]:
    """Remove exactly what `art` records. Returns {removed: [...], left: [...]}.

    Every recorded file is confined to the skill's install directory before it is touched; an entry that is not
    (tampered marker/record) is skipped and logged, never unlinked."""
    removed: list[str] = []
    left: list[str] = []
    if art["type"] == "skill_dir":
        target = Path(art["path"])
        listed = art.get("files", [])
        if not isinstance(listed, list):
            listed = []
        safe_rels: list[str] = []
        for rel in [*listed, MARKER]:
            f = confined_file(target, rel)
            if f is None:
                log.warning("skill uninstall: skipping unsafe recorded path %r (not inside %s)", rel, target)
                left.append(f"{rel!r} (outside the skill folder; skipped)")
                continue
            safe_rels.append(rel)
            if f.is_file() or f.is_symlink():
                try:
                    f.unlink()
                    removed.append(str(f))
                except OSError:
                    left.append(str(f))
        # prune directories we emptied, deepest first (only those derived from confined entries)
        dirs = sorted({target.joinpath(*Path(r).parts[:i]) for r in safe_rels for i in range(len(Path(r).parts))}, key=lambda d: -len(d.parts))
        for d in dirs:
            if d != target and d.is_dir() and not d.is_symlink():
                _rmdir_if_empty(d)
        if target.is_dir():
            if not _rmdir_if_empty(target):
                left.append(str(target))
        for d in reversed(art.get("created_dirs", [])):
            if Path(d) != target:
                _rmdir_if_empty(Path(d))
    elif art["type"] == "mcp_entry":
        path = Path(art["path"])
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                data = None
            servers = data.get("mcpServers") if isinstance(data, dict) else None
            if isinstance(servers, dict) and art["server"] in servers:
                if servers[art["server"]] == art["entry"]:
                    del servers[art["server"]]
                    removed.append(f"{path}#mcpServers.{art['server']}")
                    if art.get("created_file") and not servers and set(data) <= {"mcpServers"}:
                        path.unlink()
                    else:
                        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                else:
                    left.append(f"{path}#mcpServers.{art['server']} (modified since activation; left in place)")
    return {"removed": removed, "left": left}


# ---- MCP -------------------------------------------------------------------


def derive_mcp_config(install_method: dict[str, Any], skill_id: str, *, platform: str = sys.platform) -> Optional[dict[str, Any]]:
    """The MCP server (name + .mcp.json entry) from the catalog's documented install method, or None.

    Supported: a structured `mcp: {name, command, args, env}` / `{name, url}`, or the documented
    `claude mcp add [-e KEY=VAL]... [--transport http] <name> [--] <command> [args...]` command line.
    Other documented methods (setup wizards such as `npx ctx7 setup`) install on their own and are NOT run."""
    structured = install_method.get("mcp")
    if isinstance(structured, dict) and structured.get("name"):
        return _entry(str(structured["name"]), structured.get("command"), structured.get("args") or [], structured.get("env") or [], structured.get("url"), platform)
    cmd = install_method.get("command")
    if not isinstance(cmd, str) or not cmd.strip().startswith("claude mcp add"):
        return None
    try:
        toks = shlex.split(cmd, posix=True)[3:]
    except ValueError:
        return None
    env: list[str] = []
    transport = "stdio"
    i = 0
    while i < len(toks) and toks[i].startswith("-") and toks[i] != "--":
        if toks[i] in ("-e", "--env") and i + 1 < len(toks):
            env.append(toks[i + 1].split("=", 1)[0])
            i += 2
        elif toks[i] in ("-t", "--transport") and i + 1 < len(toks):
            transport = toks[i + 1]
            i += 2
        elif toks[i] in ("-s", "--scope") and i + 1 < len(toks):
            i += 2  # the orchestrator always writes project scope
        else:
            i += 1
    if i >= len(toks):
        return None
    name, rest = toks[i], toks[i + 1 :]
    if rest and rest[0] == "--":
        rest = rest[1:]
    if not rest:
        return None
    if transport in ("http", "sse"):
        return _entry(name, None, [], env, rest[0], platform, transport)
    return _entry(name, rest[0], rest[1:], env, None, platform)


def _entry(name: str, command: Any, args: list[Any], env: list[Any], url: Any, platform: str, transport: str = "http") -> dict[str, Any]:
    n = safe_name(name)
    entry: dict[str, Any]
    if url:
        entry = {"type": transport, "url": str(url)}
    else:
        c, a = str(command), [str(x) for x in args]
        if platform.startswith("win") and Path(c).name.lower() in ("npx", "npm", "pnpm", "yarn", "uvx"):
            c, a = "cmd", ["/c", c, *a]  # Claude Code on native Windows needs the cmd /c wrapper for npx
        entry = {"command": c, "args": a}
    if env:
        entry["env"] = {str(k): "${" + str(k) + "}" for k in env}  # placeholders only, never real secrets
    return {"name": n, "entry": entry}


def write_mcp_entry(project_root: str, server: str, entry: dict[str, Any]) -> dict[str, Any]:
    path = _inside(project_root, Path(project_root) / ".mcp.json")
    created_file = not path.exists()
    data: dict[str, Any] = {}
    if not created_file:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ActivationRefused(f"{path} is not valid JSON; refusing to modify it") from exc
        if not isinstance(data, dict):
            raise ActivationRefused(f"{path} is not a JSON object; refusing to modify it")
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ActivationRefused(f"{path}: mcpServers is not an object; refusing to modify it")
    if server in servers:
        raise ActivationRefused(f"MCP server {server!r} is already defined in {path}; refusing to overwrite it", details={"path": str(path)})
    servers[server] = entry
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {"type": "mcp_entry", "target": "claude", "path": str(path), "server": server, "entry": entry, "created_file": created_file}
