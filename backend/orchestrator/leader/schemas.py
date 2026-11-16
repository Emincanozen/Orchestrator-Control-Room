"""Schema-constrained outputs of the leader and the advisor (ARCHITECTURE section 10, ADR-008).

Every leader/advisor call asks the provider for one JSON object (`*_SCHEMA`, passed as the adapter's
json_schema) and the answer is validated STRICTLY here (`parse_output`): unknown keys, wrong types and
out-of-range sizes are errors, never silently coerced. Model output is data, never instructions.
"""

from __future__ import annotations

import json
from typing import Any, Literal, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from orchestrator.providers.base import ExecutionResult

MAX_ITEMS = 30
MAX_TASKS = 30
MAX_TEXT = 2000
TASK_TYPES = frozenset(
    {"planning", "architecture", "coding", "frontend", "testing", "review", "research", "documentation", "visual_asset", "browser_check"}
)
ROLES = frozenset(
    {"leader", "advisor", "core_systems", "backend", "frontend", "pixel_office", "test", "security", "research", "visual"}
)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _bounded_strings(v: list[str], *, max_items: int = MAX_ITEMS, name: str = "list") -> list[str]:
    if len(v) > max_items:
        raise ValueError(f"{name} has {len(v)} items (max {max_items})")
    for s in v:
        if len(s) > MAX_TEXT:
            raise ValueError(f"{name} item longer than {MAX_TEXT} chars")
    return v


# ---------------------------------------------------------------------------- structured checks
class CheckSpec(Strict):
    """One structured verification check proposed by the leader. Exactly one kind per object; unused keys are
    null (the JSON schema lists every key as required so strict-schema providers accept it)."""

    file_exists: Optional[str] = None
    contains: Optional[str] = None
    file_absent: Optional[str] = None
    file_unchanged: Optional[str] = None
    json_valid: Optional[str] = None
    cmd: Optional[list[str]] = None

    @model_validator(mode="after")
    def _one_kind(self) -> "CheckSpec":
        kinds = [k for k in ("file_exists", "file_absent", "file_unchanged", "json_valid", "cmd") if getattr(self, k) is not None]
        if len(kinds) != 1:
            raise ValueError("a check must use exactly one of file_exists, file_absent, file_unchanged, json_valid, cmd")
        if self.contains is not None and self.file_exists is None:
            raise ValueError("contains is only valid together with file_exists")
        if self.cmd is not None and not self.cmd:
            raise ValueError("cmd must be a non-empty argv list")
        return self

    def to_spec(self) -> dict[str, Any]:
        return {k: v for k, v in self.model_dump().items() if v is not None}


MAX_CHECKS = 20


CHECK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "file_exists": {"type": ["string", "null"], "description": "relative path that must exist"},
        "contains": {"type": ["string", "null"], "description": "with file_exists: text the file must contain"},
        "file_absent": {"type": ["string", "null"], "description": "relative path that must NOT exist"},
        "file_unchanged": {"type": ["string", "null"], "description": "relative path that must equal its base-commit version"},
        "json_valid": {"type": ["string", "null"], "description": "relative path of a file that must parse as JSON"},
        "cmd": {"type": ["array", "null"], "items": {"type": "string"}, "description": "argv list of an allowlisted test command"},
    },
    "required": ["file_exists", "contains", "file_absent", "file_unchanged", "json_valid", "cmd"],
}
CHECKS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": CHECK_SCHEMA,
    "description": "structured verification checks (preferred over verify_commands); one kind per object, others null",
}


# ---------------------------------------------------------------------------- plan
class PlanResult(Strict):
    deliverables: list[str]
    prerequisites: list[str]
    acceptance_criteria: list[str] = Field(min_length=1)
    steps: list[str] = Field(min_length=1)
    verify_commands: list[str]
    risks: list[str]
    needs_user_input: list[str]
    checks: list[CheckSpec] = Field(default_factory=list, max_length=MAX_CHECKS)

    @field_validator("deliverables", "prerequisites", "acceptance_criteria", "steps", "verify_commands", "risks", "needs_user_input")
    @classmethod
    def _bound(cls, v: list[str]) -> list[str]:
        return _bounded_strings(v)



def _arr(desc: str = "") -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": desc}


PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "deliverables": _arr("what will exist when the task is done"),
        "prerequisites": _arr("what must already be true"),
        "acceptance_criteria": _arr("verifiable conditions for done (at least one)"),
        "steps": _arr("ordered implementation steps (at least one)"),
        "verify_commands": _arr("only real test/lint/build command lines from the allowlist, e.g. 'uv run pytest -q'; else empty"),
        "risks": _arr(),
        "needs_user_input": _arr("questions only a human can answer; empty when none"),
        "checks": CHECKS_SCHEMA,
    },
    "required": ["deliverables", "prerequisites", "acceptance_criteria", "steps", "verify_commands", "risks", "needs_user_input", "checks"],
}


# ---------------------------------------------------------------------------- review
class ReviewResult(Strict):
    verdict: Literal["approve", "request_changes", "escalate"]
    findings: list[str]
    required_changes: list[str]
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("findings", "required_changes")
    @classmethod
    def _bound(cls, v: list[str]) -> list[str]:
        return _bounded_strings(v)

    @model_validator(mode="after")
    def _changes_needed(self) -> "ReviewResult":
        if self.verdict == "request_changes" and not (self.required_changes or self.findings):
            raise ValueError("request_changes needs findings or required_changes")
        return self


REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "request_changes", "escalate"]},
        "findings": _arr(),
        "required_changes": _arr("concrete changes required before approval"),
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["verdict", "findings", "required_changes", "confidence"],
}


# ---------------------------------------------------------------------------- leader decision after advice
class FinalDecision(Strict):
    verdict: Literal["approve", "request_changes"]
    rationale: str = Field(min_length=1, max_length=MAX_TEXT * 2)
    evidence_refs: list[str]
    required_changes: list[str]

    @field_validator("evidence_refs", "required_changes")
    @classmethod
    def _bound(cls, v: list[str]) -> list[str]:
        return _bounded_strings(v)


FINAL_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "request_changes"]},
        "rationale": {"type": "string"},
        "evidence_refs": _arr("ids from the evidence list you rely on (verification id, diff ref, run id)"),
        "required_changes": _arr(),
    },
    "required": ["verdict", "rationale", "evidence_refs", "required_changes"],
}


# ---------------------------------------------------------------------------- objective decomposition
class TaskSpecOut(Strict):
    key: str = Field(min_length=1, max_length=60, pattern=r"^[A-Za-z0-9_.-]+$")
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(max_length=MAX_TEXT * 4)
    task_type: str
    role: str
    priority: int = Field(ge=0, le=100)
    depends_on: list[str]
    acceptance_criteria: list[str]
    verify_commands: list[str]
    checks: list[CheckSpec] = Field(default_factory=list, max_length=MAX_CHECKS)

    @field_validator("depends_on", "acceptance_criteria", "verify_commands")
    @classmethod
    def _bound(cls, v: list[str]) -> list[str]:
        return _bounded_strings(v)



class Decomposition(Strict):
    deliverables: list[str]
    prerequisites: list[str]
    acceptance_criteria: list[str]
    tasks: list[TaskSpecOut] = Field(min_length=1, max_length=MAX_TASKS)

    @field_validator("deliverables", "prerequisites", "acceptance_criteria")
    @classmethod
    def _bound(cls, v: list[str]) -> list[str]:
        return _bounded_strings(v)

    def check_graph(self) -> None:
        """Semantic validation: unique keys, known deps (no self-dependency), no cycles, known types/roles."""
        keys = [t.key for t in self.tasks]
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        if dupes:
            raise ValueError(f"duplicate task keys: {dupes}")
        known = set(keys)
        for t in self.tasks:
            if t.task_type not in TASK_TYPES:
                raise ValueError(f"task {t.key}: unknown task_type {t.task_type!r} (known: {sorted(TASK_TYPES)})")
            if t.role not in ROLES:
                raise ValueError(f"task {t.key}: unknown role {t.role!r} (known: {sorted(ROLES)})")
            if t.key in t.depends_on:
                raise ValueError(f"task {t.key} depends on itself")
            missing = sorted(set(t.depends_on) - known)
            if missing:
                raise ValueError(f"task {t.key}: depends_on unknown keys {missing}")
        # Kahn's algorithm: whatever cannot be ordered is on (or behind) a cycle
        indeg = {t.key: len(set(t.depends_on)) for t in self.tasks}
        rev: dict[str, list[str]] = {k: [] for k in keys}
        for t in self.tasks:
            for d in set(t.depends_on):
                rev[d].append(t.key)
        ready = [k for k, n in indeg.items() if n == 0]
        seen = 0
        while ready:
            k = ready.pop()
            seen += 1
            for nxt in rev[k]:
                indeg[nxt] -= 1
                if indeg[nxt] == 0:
                    ready.append(nxt)
        if seen != len(keys):
            cyc = sorted(k for k, n in indeg.items() if n > 0)
            raise ValueError(f"dependency cycle among tasks: {cyc}")


DECOMPOSITION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "deliverables": _arr(),
        "prerequisites": _arr(),
        "acceptance_criteria": _arr(),
        "tasks": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_TASKS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "key": {"type": "string", "description": "short unique id, letters/digits/_-. only"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "task_type": {"type": "string", "enum": sorted(TASK_TYPES)},
                    "role": {"type": "string", "enum": sorted(ROLES)},
                    "priority": {"type": "integer", "minimum": 0, "maximum": 100},
                    "depends_on": _arr("keys of tasks that must complete first"),
                    "acceptance_criteria": _arr(),
                    "verify_commands": _arr("only real test/lint/build command lines from the allowlist, e.g. 'uv run pytest -q'; else empty"),
                    "checks": CHECKS_SCHEMA,
                },
                "required": [
                    "key", "title", "description", "task_type", "role", "priority", "depends_on",
                    "acceptance_criteria", "verify_commands", "checks",
                ],
            },
        },
    },
    "required": ["deliverables", "prerequisites", "acceptance_criteria", "tasks"],
}


# ---------------------------------------------------------------------------- advisor
class AdvisorResult(Strict):
    assessment: str = Field(min_length=1, max_length=MAX_TEXT * 4)
    recommended_option: str = Field(min_length=1, max_length=MAX_TEXT)
    risks: list[str]
    disagreements: list[str]
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_requested: list[str]

    @field_validator("risks", "disagreements", "evidence_requested")
    @classmethod
    def _bound(cls, v: list[str]) -> list[str]:
        return _bounded_strings(v)


ADVISOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "assessment": {"type": "string"},
        "recommended_option": {"type": "string", "description": "one of the candidate options, or your own if none fits"},
        "risks": _arr(),
        "disagreements": _arr("where you disagree with a candidate option or requirement"),
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence_requested": _arr("evidence that would change your recommendation"),
    },
    "required": ["assessment", "recommended_option", "risks", "disagreements", "confidence", "evidence_requested"],
}


# ---------------------------------------------------------------------------- leader switch validation
class SwitchTaskState(Strict):
    id: str
    next_action: str = Field(min_length=1, max_length=MAX_TEXT)


class SwitchValidation(Strict):
    tasks: list[SwitchTaskState]

    def check_ids(self, expected: set[str]) -> None:
        ids = [t.id for t in self.tasks]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate task ids in answer: {dupes}")
        got = set(ids)
        if got != expected:
            raise ValueError(f"task ids do not match the checkpoint: missing {sorted(expected - got)}, unexpected {sorted(got - expected)}")


SWITCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"id": {"type": "string"}, "next_action": {"type": "string"}},
                "required": ["id", "next_action"],
            },
        }
    },
    "required": ["tasks"],
}


# ---------------------------------------------------------------------------- parsing
def _first_object(text: str) -> Optional[dict[str, Any]]:
    """The first JSON object embedded in `text` (models sometimes wrap it in prose or a code fence)."""
    dec = json.JSONDecoder()
    start = text.find("{")
    attempts = 0
    while start != -1 and attempts < 50:
        attempts += 1
        try:
            obj, _end = dec.raw_decode(text, start)
        except ValueError:
            start = text.find("{", start + 1)
            continue
        if isinstance(obj, dict):
            return obj
        start = text.find("{", start + 1)
    return None


def raw_json(result: ExecutionResult) -> Optional[dict[str, Any]]:
    if isinstance(result.structured_output, dict):
        return result.structured_output
    return _first_object(result.final_text or "")


M = TypeVar("M", bound=BaseModel)


def parse_output(result: ExecutionResult, model: type[M]) -> tuple[Optional[M], Optional[str]]:
    """(validated object, None) or (None, short reason). Never raises."""
    data = raw_json(result)
    if data is None:
        return None, "no JSON object found in the output"
    try:
        return model.model_validate(data), None
    except ValidationError as exc:
        parts = []
        for e in exc.errors()[:6]:
            loc = ".".join(str(x) for x in e.get("loc", ())) or "(root)"
            parts.append(f"{loc}: {e.get('msg')}")
        return None, "; ".join(parts)
    except ValueError as exc:
        return None, str(exc)
