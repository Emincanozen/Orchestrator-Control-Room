"""One schema-constrained call with strict validation and a bounded repair attempt.

`structured_call` asks, validates, and on invalid output asks ONCE more (the rejection reason is fed
back); a second invalid answer is a failure with a clear reason. The caller supplies `call(prompt)` so the
same logic serves executor-run phases (PhaseRunner) and CallRunner calls, and `allow_attempt(n)` so a
call budget (max_leader_calls_per_task) is enforced before every attempt, repair included.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Generic, Optional, TypeVar

from pydantic import BaseModel

from orchestrator.leader.schemas import parse_output
from orchestrator.providers.base import ExecutionResult

M = TypeVar("M", bound=BaseModel)

MAX_ATTEMPTS = 2  # first answer + exactly one repair attempt


@dataclass
class Structured(Generic[M]):
    obj: Optional[M] = None
    error: Optional[str] = None  # why it failed (clear, human readable)
    kind: str = "ok"  # ok | call_failed | invalid | budget
    result: Optional[ExecutionResult] = None  # the last provider result
    metas: list[Any] = field(default_factory=list)  # whatever `call` returned alongside each result (run ids, ...)
    attempts: int = 0


def repair_prompt(prompt: str, error: str) -> str:
    return (
        f"{prompt}\n\nYour previous answer was rejected: {error}\n"
        "Reply again with ONLY one JSON object that matches the schema exactly (no prose, no code fence)."
    )


async def structured_call(
    call: Callable[[str], Awaitable[tuple[ExecutionResult, Any]]],
    model: type[M],
    prompt: str,
    *,
    allow_attempt: Optional[Callable[[int], Awaitable[bool]]] = None,
    post_validate: Optional[Callable[[M], Optional[str]]] = None,
    what: str = "output",
) -> Structured[M]:
    out: Structured[M] = Structured()
    current = prompt
    last_error = ""
    for n in range(MAX_ATTEMPTS):
        if allow_attempt is not None and not await allow_attempt(n):
            out.kind, out.error = "budget", f"call budget exhausted before attempt {n + 1}" + (f" (last problem: {last_error})" if last_error else "")
            return out
        result, meta = await call(current)
        out.result, out.attempts = result, n + 1
        out.metas.append(meta)
        if not result.success:
            out.kind = "call_failed"
            out.error = result.error_message or (result.error_category.value if result.error_category else "provider call failed")
            return out
        obj, err = parse_output(result, model)
        if obj is not None and post_validate is not None:
            err = post_validate(obj)  # semantic checks (graph shape, ids ...) get the same one repair attempt
            if err is not None:
                obj = None
        if obj is not None:
            out.obj, out.kind, out.error = obj, "ok", None
            return out
        last_error = err or "invalid output"
        current = repair_prompt(prompt, last_error)
    out.kind = "invalid"
    out.error = f"{what} is invalid after {MAX_ATTEMPTS} attempts (1 repair): {last_error}"
    return out
