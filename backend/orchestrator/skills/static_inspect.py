"""Static inspection of a downloaded skill package. Reads files only; never executes anything."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

import yaml

SCRIPT_EXT = {".sh", ".bash", ".zsh", ".ps1", ".psm1", ".bat", ".cmd", ".py", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl", ".vbs", ".jar"}
BINARY_EXT = {".exe", ".dll", ".msi", ".com", ".scr", ".so", ".dylib", ".node", ".bin"}
LIFECYCLE = ("preinstall", "install", "postinstall", "prepare", "prepublish", "prepack")
SHELLS = {"sh", "bash", "zsh", "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe"}
MAX_PARSE_BYTES = 1_000_000
LISTING_LIMIT = 200
_SUSPICIOUS = [
    (re.compile(r"ignore (all )?(the )?(previous|prior|above) instructions", re.I), "prompt-injection phrase"),
    (re.compile(r"(curl|wget)[^\n|]*\|\s*(sudo\s+)?(ba)?sh", re.I), "pipe-to-shell download"),
    (re.compile(r"\b(iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b[^\n|]*\|\s*(iex|Invoke-Expression)", re.I), "download-and-execute (PowerShell)"),
    (re.compile(r"\brm\s+-rf\s+(/|~|\$HOME)", re.I), "destructive rm -rf"),
]
_RANK = {"low": 0, "medium": 1, "unknown": 2, "high": 3}
_ENV_SECRET = re.compile(r"(api[_-]?key|token|secret|password|credential)", re.I)


def _finding(sev: str, code: str, path: str, message: str) -> dict[str, str]:
    return {"severity": sev, "code": code, "path": path, "message": message}


def parse_frontmatter(text: str) -> tuple[dict[str, Any], Optional[str]]:
    m = re.match(r"\A﻿?---\r?\n(.*?)\r?\n---\s*(?:\r?\n|\Z)", text, re.S)
    if not m:
        return {}, "no YAML frontmatter"
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError as exc:
        return {}, f"invalid frontmatter: {str(exc)[:120]}"
    return (data if isinstance(data, dict) else {}), (None if isinstance(data, dict) else "frontmatter is not a mapping")


def _read_text(p: Path) -> str:
    return p.read_bytes()[:MAX_PARSE_BYTES].decode("utf-8", errors="replace")


def inspect_package(root: Path, *, catalog_risk: str = "unknown", catalog_notes: str = "", catalog_permissions: Optional[list[str]] = None,
                    catalog_requires_credentials: bool = False) -> dict[str, Any]:
    """Return the `security` report: {risk, findings[], permissions[], requires_credentials, ...}."""
    findings: list[dict[str, str]] = []
    skill_md: list[dict[str, Any]] = []
    scripts: list[str] = []
    binaries: list[str] = []
    package_scripts: list[dict[str, Any]] = []
    mcp_servers: list[dict[str, Any]] = []
    hooks: list[str] = []
    listing: list[str] = []
    credentials = bool(catalog_requires_credentials)
    count = total = 0

    for p in sorted(root.rglob("*")):
        if p.is_symlink():  # cannot happen after a safe fetch; flag if it does
            findings.append(_finding("high", "symlink", p.relative_to(root).as_posix(), "symlink present in package"))
            continue
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        low = rel.lower()
        parts = low.split("/")
        size = p.stat().st_size
        count += 1
        total += size
        if len(listing) < LISTING_LIMIT:
            listing.append(rel)
        ext = p.suffix.lower()
        name = p.name.lower()

        if name == "skill.md":
            fm, err = parse_frontmatter(_read_text(p))
            skill_md.append({"path": rel, "name": fm.get("name"), "description": fm.get("description"), "frontmatter_error": err})
            if err:
                findings.append(_finding("low", "skill_md_frontmatter", rel, err))
            if "hooks" in fm:
                hooks.append(rel)
                findings.append(_finding("high", "hooks", rel, "SKILL.md frontmatter declares hooks"))
            if fm.get("allowed-tools") or fm.get("allowed_tools"):
                findings.append(_finding("low", "allowed_tools", rel, f"declares allowed-tools: {fm.get('allowed-tools') or fm.get('allowed_tools')}"))
        if ext in BINARY_EXT:
            binaries.append(rel)
            findings.append(_finding("high", "native_binary", rel, "native executable / library"))
        elif ext in SCRIPT_EXT:
            scripts.append(rel)
        if "hooks" in parts[:-1] or ".husky" in parts or name in ("hooks.json",):
            hooks.append(rel)
            findings.append(_finding("high", "hooks", rel, "hook definition / hook script"))
        if name == "package.json":
            try:
                pkg = json.loads(_read_text(p))
            except ValueError:
                findings.append(_finding("low", "package_json", rel, "unparseable package.json"))
                pkg = {}
            for key, cmd in (pkg.get("scripts") or {}).items() if isinstance(pkg.get("scripts"), dict) else []:
                package_scripts.append({"path": rel, "name": key, "command": str(cmd)[:200]})
                if key in LIFECYCLE:
                    findings.append(_finding("high", "npm_lifecycle_script", rel, f"package.json `{key}` runs on install: {str(cmd)[:120]}"))
            if pkg.get("bin"):
                findings.append(_finding("medium", "npm_bin", rel, "package.json declares executables (bin)"))
        if name in (".mcp.json", "mcp.json") or (name in ("settings.json", "settings.local.json", "plugin.json") and ".claude" in low):
            try:
                data = json.loads(_read_text(p))
            except ValueError:
                data = {}
            if isinstance(data, dict):
                if data.get("hooks"):
                    hooks.append(rel)
                    findings.append(_finding("high", "hooks", rel, "configuration defines hooks"))
                servers = data.get("mcpServers") or data.get("servers") or {}
                for sname, sdef in servers.items() if isinstance(servers, dict) else []:
                    if not isinstance(sdef, dict):
                        continue
                    cmd = sdef.get("command") or sdef.get("url") or ""
                    mcp_servers.append({"path": rel, "name": sname, "command": str(cmd)[:200], "args": [str(a)[:80] for a in (sdef.get("args") or [])][:10]})
                    shell = Path(str(cmd)).name.lower() in SHELLS
                    findings.append(_finding("high" if shell else "medium", "mcp_server_definition", rel, f"defines MCP server `{sname}`: {str(cmd)[:100]}"))
                    for env_key in (sdef.get("env") or {}) if isinstance(sdef.get("env"), dict) else []:
                        if _ENV_SECRET.search(str(env_key)):
                            credentials = True
        if ext in (".md", ".txt", ".sh", ".ps1", ".bat", ".cmd", ".py", ".js") and size <= MAX_PARSE_BYTES:
            text = _read_text(p)
            for rx, label in _SUSPICIOUS:
                if rx.search(text):
                    findings.append(_finding("medium", "suspicious_text", rel, label))
                    break

    if scripts:
        findings.append(_finding("medium", "executable_scripts", ", ".join(scripts[:5]) + (" ..." if len(scripts) > 5 else ""), f"{len(scripts)} script file(s); never run by the orchestrator"))

    inspected = "low"
    if mcp_servers or scripts or package_scripts:
        inspected = "medium"
    if binaries or hooks or any(f["severity"] == "high" for f in findings):
        inspected = "high"
    risk = catalog_risk if _RANK.get(catalog_risk, 2) >= _RANK[inspected] else inspected
    if risk not in _RANK:
        risk = "unknown"

    permissions = list(dict.fromkeys(catalog_permissions or []))
    for flag, perm in ((scripts or package_scripts, "executes_scripts"), (binaries, "native_binaries"), (hooks, "hooks"), (mcp_servers, "mcp_server")):
        if flag and perm not in permissions:
            permissions.append(perm)

    reasons: list[str] = []
    if risk in ("high", "unknown"):
        reasons.append(f"risk is {risk}")
    if credentials:
        reasons.append("requires credentials")
    if scripts or package_scripts or binaries:
        reasons.append("contains executable scripts / binaries")
    if hooks:
        reasons.append("contains hooks")
    if mcp_servers:
        reasons.append("defines MCP server commands")

    primary = next((s["name"] for s in skill_md if s["path"].lower() == "skill.md" and s.get("name")), None)
    return {
        "inspected": True,
        "risk": risk,
        "catalog_risk": catalog_risk,
        "catalog_notes": catalog_notes,
        "findings": findings,
        "permissions": permissions,
        "requires_credentials": credentials,
        "needs_review": bool(reasons),
        "review_reasons": reasons,
        "primary_skill_name": primary,
        "has_root_skill_md": any(s["path"].lower() == "skill.md" for s in skill_md),
        "skill_md": skill_md[:50],
        "scripts": scripts[:100],
        "native_binaries": binaries[:50],
        "package_scripts": package_scripts[:50],
        "mcp_servers": mcp_servers[:50],
        "hooks": sorted(set(hooks))[:50],
        "files": {"count": count, "bytes": total, "listing": listing},
    }


def pending_security(catalog_risk: str, notes: str, permissions: list[str], requires_credentials: bool) -> dict[str, Any]:
    """The `security` value before anything was downloaded (catalog knowledge only)."""
    return {
        "inspected": False,
        "risk": catalog_risk,
        "catalog_risk": catalog_risk,
        "catalog_notes": notes,
        "findings": [],
        "permissions": list(permissions),
        "requires_credentials": requires_credentials,
        "needs_review": catalog_risk in ("high", "unknown") or requires_credentials,
        "review_reasons": [],
    }
