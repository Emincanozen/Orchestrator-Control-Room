"""Compatibility checks (declared dependencies, platform flags) and Codex support."""

from __future__ import annotations

import re
import shutil
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# tool name in free-text dependency -> executables that satisfy it
KNOWN_TOOLS: dict[str, tuple[str, ...]] = {
    "node": ("node",),
    "npm": ("npm",),
    "npx": ("npx",),
    "python": ("python", "python3", "py"),
    "uv": ("uv",),
    "pip": ("pip", "pip3"),
    "ffmpeg": ("ffmpeg",),
    "docker": ("docker",),
    "git": ("git",),
    "yt-dlp": ("yt-dlp",),
}
# Phrases that make a free-text dependency advisory ("unverified", "likely", "for some sources", ...).
_SOFT = re.compile(r"unverified|likely|per listings|per scripts|for some|optional|for the |for live|rendering|general .* requirement|example", re.I)

CODEX_AGENTS_SKILLS_NOTE = (
    "Codex project-level skill discovery is NOT confirmed. `codex --help` / `codex features list` document no skills "
    "location, and the only evidence on this machine is string constants inside the codex binary (a discovery "
    "candidate named '.agents/skills' next to 'codex-home-skills' and 'user-agent-skills'), which does not establish "
    "that a project-level `<project>/.agents/skills` folder is read. Codex activation is therefore unsupported."
)


@dataclass
class CompatReport:
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blockers


def _tool_in(text: str) -> Optional[str]:
    low = text.lower()
    for tool in KNOWN_TOOLS:
        if re.search(rf"(?<![\w-]){re.escape(tool)}(?![\w-])", low):
            return tool
    return None


def check_compat(
    compat: dict[str, Any],
    dependencies: list[str],
    *,
    which: Callable[[str], Optional[str]] = shutil.which,
    platform: str = sys.platform,
) -> CompatReport:
    """Blockers make the skill INCOMPATIBLE; warnings are advisory. A dependency is a blocker only when it is
    stated plainly (e.g. "node>=18", "bin:ffmpeg"); hedged catalog text ("node (unverified)") is a warning."""
    rep = CompatReport()
    if str(compat.get("claude_code", "")).lower() == "no":
        rep.blockers.append("declared not compatible with Claude Code")
    if platform.startswith("win") and str(compat.get("windows", "")).lower() == "no":
        rep.blockers.append("declared not compatible with Windows")
    for dep in dependencies:
        text = str(dep).strip()
        if not text:
            continue
        if re.search(r"\b(macos|mac os|linux)[- ]only\b", text, re.I) and platform.startswith("win"):
            rep.blockers.append(f"{text}: not available on Windows")
            continue
        explicit = text.lower().startswith("bin:")
        tool = text[4:].strip() if explicit else _tool_in(text)
        if not tool:
            continue
        candidates = KNOWN_TOOLS.get(tool, (tool,))
        if any(which(c) for c in candidates):
            continue
        if explicit or not _SOFT.search(text):
            rep.blockers.append(f"required tool not found on PATH: {tool} ({text})")
        else:
            rep.warnings.append(f"tool not found on PATH: {tool} ({text})")
    return rep


def codex_support(enabled: bool) -> dict[str, Any]:
    if enabled:
        return {"supported": True, "location": ".agents/skills", "note": "enabled by configuration (unconfirmed upstream)"}
    return {"supported": False, "reason": CODEX_AGENTS_SKILLS_NOTE}
