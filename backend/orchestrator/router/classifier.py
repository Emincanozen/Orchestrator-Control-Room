"""Deterministic, explainable task classification. Keyword/heuristic only; no LLM calls."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

Level = Literal["low", "medium", "high", "critical"]
LEVELS: tuple[str, ...] = ("low", "medium", "high", "critical")


class TaskClassification(BaseModel):
    task_type: str
    complexity: Level
    risk: Level
    required_capabilities: list[str] = Field(default_factory=list)
    signals: list[str] = Field(default_factory=list)
    role: str | None = None  # only set when the caller gave an explicit role
    # Capabilities the caller asked for explicitly (hints) vs. inferred from keywords in the task text.
    # The router may ignore *inferred* ones to honour an explicit preferred provider; explicit ones never.
    explicit_capabilities: list[str] = Field(default_factory=list)
    inferred_capabilities: list[str] = Field(default_factory=list)


def _rx(p: str) -> re.Pattern[str]:
    return re.compile(p, re.IGNORECASE)


# --- visual / capability rules (kept distinct on purpose) -------------------------------------------------
CAPABILITY_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("image_generation", _rx(
        r"\b(generate|create|draw|produce|render|make|design)\b[^.\n]{0,40}\b(images?|logos?|illustrations?|pictures?"
        r"|artworks?|posters?|banners?|mascots?|avatars?)\b|\b(text[- ]to[- ]image|image generation|dall-?e)\b")),
    ("image_editing", _rx(
        r"\b(edit|retouch|crop|resize|recolou?r|inpaint|upscale|touch up)\b[^.\n]{0,40}"
        r"\b(images?|photos?|pictures?|logo|illustration)\b|\bremove (the )?background\b")),
    ("screenshot_analysis", _rx(
        r"\b(analy[sz]e|inspect|read|review|compare|describe|check|look at|understand|extract|interpret)\b[^.\n]{0,60}"
        r"\bscreenshots?\b|\bscreenshots?\b[^.\n]{0,40}\b(analysis|analy[sz]e|shows?|reveals?)\b")),
    ("image_understanding", _rx(
        r"\b(analy[sz]e|describe|interpret|understand|caption|ocr|extract text from|read)\b[^.\n]{0,40}"
        r"\b(images?|photos?|pictures?|diagrams?|charts?|figures?)\b")),
    ("ui_implementation", _rx(
        r"\bui\b|\bfrontend\b|\bfront-end\b|\bcss\b|\btailwind\b|\b(react|vue|svelte|ui|frontend|front-end) components?\b"
        r"|\b(settings|login|home|dashboard|landing|profile)\s+(page|screen|view)\b|\bmodal\b|\bnavbar\b|\bsidebar\b"
        r"|\bstylesheet\b|\bresponsive layout\b")),
    ("visual_design", _rx(
        r"\b(mockups?|wireframes?|design system|visual design|colou?r palette|brand identity|moodboard|figma|style guide)\b")),
    ("browser_inspection", _rx(
        r"\b(in the browser|browser|playwright|puppeteer|selenium|headless|dom|rendered page|inspect the page"
        r"|open the page|visual check|visually (check|inspect|verify))\b")),
    ("pixel_art", _rx(r"\bpixel[- ]?art\b|\bsprite ?sheets?\b|\b(8|16)-?bit\b|\btilesets?\b")),
    ("animation", _rx(r"\banimat\w*|\btween\w*|\bkeyframes?\b|\blottie\b|\bframe[- ]by[- ]frame\b")),
    ("video_generation", _rx(
        r"\b(generate|create|render|produce)\b[^.\n]{0,30}\bvideos?\b|\btext[- ]to[- ]video\b|\bvideo generation\b")),
]
VISUAL_ONLY_CAPS = frozenset({
    "image_generation", "image_editing", "screenshot_analysis", "image_understanding",
    "visual_design", "pixel_art", "animation", "video_generation",
})
CODE_HINT = _rx(
    r"\b(code|implement|function|class|api|pixi\w*|three\.?js|canvas|webgl|react|typescript|javascript|python|css|html"
    r"|library|framework|game|script|module|endpoint|backend|database)\b")

TASK_TYPE_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("architecture", _rx(r"\b(architecture|architect|adr|system design|design the system)\b")),
    ("review", _rx(r"\b(review|audit|code review|security review|assess)\b")),
    ("testing", _rx(r"\b(tests?|testing|unit tests?|test coverage|pytest|regression)\b")),
    ("planning", _rx(r"\b(plan|planning|roadmap|break ?down|decompose|milestones?|strategy)\b")),
    ("documentation", _rx(r"\b(document|documentation|docs|readme|changelog|docstrings?)\b")),
    ("research", _rx(r"\b(research|investigate|compare|survey|find out|evaluate options|look into|explore options)\b")),
]
BASE_CAPS: dict[str, list[str]] = {
    "coding": ["coding"], "testing": ["coding"], "frontend": ["coding", "ui_implementation"],
    "planning": ["planning"], "architecture": ["planning"], "review": ["review"], "research": ["research"],
    "documentation": ["documentation"], "browser_check": ["browser_inspection"], "visual_asset": [],
}

COMPLEXITY_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("critical", _rx(r"\b(mission[- ]critical|rewrite the entire|from scratch.{0,30}(platform|system)|zero[- ]downtime)\b")),
    ("high", _rx(
        r"\b(refactor|migrat\w+|redesign|re-?architect|end[- ]to[- ]end|distributed|concurren\w+|multi[- ]\w+"
        r"|across (the )?(codebase|modules|services)|performance|optimi[sz]e|scheduler|state machine|architecture)\b")),
    ("low", _rx(r"\b(typo|rename|tiny|trivial|one[- ]liner|simple|minor|small|bump version|fix comment|wording|readme)\b")),
]
RISK_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("critical", _rx(
        r"\b(delete (the )?production|drop (table|database)|rm -rf|force[- ]push|reset --hard|deploy\w* to prod\w*"
        r"|production deploy\w*|payments?|purchase|billing|rotate (the )?(credentials|keys|secrets))\b")),
    ("high", _rx(
        r"\b(auth\w*|security|passwords?|secrets?|tokens?|credentials?|encrypt\w*|permissions?|sandbox"
        r"|schema migration|database migration|data migration|deploy\w*|production|delete)\b")),
]


def _bump(level: str, by: int = 1) -> str:
    return LEVELS[min(len(LEVELS) - 1, LEVELS.index(level) + by)]


def _first_match(rules: list[tuple[str, re.Pattern[str]]], text: str) -> tuple[str, str] | None:
    for name, rx in rules:
        m = rx.search(text)
        if m:
            return name, m.group(0)
    return None


def classify(
    title: str,
    description: str = "",
    task_type: str | None = None,
    role: str | None = None,
    hints: dict[str, Any] | None = None,
) -> TaskClassification:
    hints = hints or {}
    title, description = title or "", description or ""
    full = f"{title}\n{description}"
    signals: list[str] = []

    # --- capabilities -------------------------------------------------------------------------------------
    detected: list[str] = []
    for cap, rx in CAPABILITY_RULES:
        m = rx.search(full)
        if m:
            detected.append(cap)
            signals.append(f"capability:{cap} matched '{m.group(0).strip()[:40]}'")
    # a screenshot task is not a generic image-understanding task
    if "screenshot_analysis" in detected and "image_understanding" in detected:
        detected.remove("image_understanding")
        signals.append("capability:image_understanding dropped (screenshot_analysis is more specific)")
    code_hint = bool(CODE_HINT.search(full))

    # --- task type ----------------------------------------------------------------------------------------
    if task_type:
        ttype = task_type
        signals.append(f"task_type:{ttype} explicit")
    elif "ui_implementation" in detected:
        ttype = "frontend"
        signals.append("task_type:frontend from ui_implementation")
    elif "browser_inspection" in detected and not (set(detected) & VISUAL_ONLY_CAPS):
        ttype = "browser_check"
        signals.append("task_type:browser_check from browser_inspection")
    elif set(detected) & VISUAL_ONLY_CAPS and not code_hint:
        ttype = "visual_asset"
        signals.append("task_type:visual_asset from visual capability without code context")
    else:
        found = _first_match(TASK_TYPE_RULES, title) or _first_match(TASK_TYPE_RULES, full)
        if found:
            ttype = found[0]
            signals.append(f"task_type:{ttype} matched '{found[1]}'")
        else:
            ttype = "coding"
            signals.append("task_type:coding default")

    caps: list[str] = list(BASE_CAPS.get(ttype, ["coding"]))
    for c in detected:
        if c not in caps:
            caps.append(c)
    if ttype == "visual_asset" and not (set(caps) & VISUAL_ONLY_CAPS):
        caps.append("image_generation")
        signals.append("capability:image_generation default for explicit visual_asset")
    explicit: list[str] = []
    for c in hints.get("required_capabilities") or []:
        explicit.append(c)
        if c not in caps:
            caps.append(c)
            signals.append(f"capability:{c} from hints")
    # inferred = keyword-detected and not guaranteed by an explicit task type or hints
    explicit_base = set(BASE_CAPS.get(ttype, [])) if task_type else set()
    inferred = [c for c in detected if c not in explicit and c not in explicit_base]

    # --- complexity ---------------------------------------------------------------------------------------
    if "complexity" in hints:
        complexity = str(hints["complexity"])
        signals.append(f"complexity:{complexity} from hints")
    else:
        complexity = "medium"
        found = _first_match(COMPLEXITY_RULES, full)
        if found:
            complexity = found[0]
            signals.append(f"complexity:{complexity} matched '{found[1]}'")
        else:
            signals.append("complexity:medium default")
        if len(full) > 1500 and complexity in ("medium", "low"):
            complexity = _bump(complexity)
            signals.append("complexity bumped one level: long description")
        if ttype == "architecture" and LEVELS.index(complexity) < 2:
            complexity = "high"
            signals.append("complexity:high architecture work")

    # --- risk ---------------------------------------------------------------------------------------------
    if "risk" in hints:
        risk = str(hints["risk"])
        signals.append(f"risk:{risk} from hints")
    else:
        risk = "low"
        found = _first_match(RISK_RULES, full)
        if found:
            risk = found[0]
            signals.append(f"risk:{risk} matched '{found[1]}'")
        else:
            signals.append("risk:low default")

    if complexity not in LEVELS or risk not in LEVELS:
        raise ValueError(f"invalid complexity/risk: {complexity!r}/{risk!r}")
    if role:
        signals.append(f"role:{role} explicit")
    return TaskClassification(
        task_type=ttype, complexity=complexity, risk=risk, required_capabilities=caps, signals=signals, role=role,
        explicit_capabilities=explicit, inferred_capabilities=inferred,
    )
