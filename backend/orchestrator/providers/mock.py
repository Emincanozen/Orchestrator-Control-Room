"""Scriptable mock provider for tests and offline demos.

Selectable at runtime only when ORCHESTRATOR_ENABLE_MOCK=1. Each `execute`
call consumes one MockScript (FIFO queue, then the optional `scripter`
callback, then `default_script`). Scripts are lists of steps:

    MockScript.success("done", files={"hello.txt": "hi"})
    MockScript.failure(ErrorCategory.RATE_LIMIT)
    MockScript.hang()          # until cancel(run_id) (or a bounded timeout)
    MockScript.crash()         # RESULT success=False, category=crashed
    MockScript(steps=[MockStep(action="raise")])   # adapter bug: raises

It never spawns processes and never spends quota.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Literal, Optional

from pydantic import BaseModel, Field

from orchestrator.providers.base import (
    AgentEvent,
    AgentEventKind,
    ErrorCategory,
    ExecutionRequest,
    ExecutionResult,
    ModelInfo,
    ProviderAdapter,
    ProviderCapabilities,
    ProviderStatus,
)

MAX_HANG_SECONDS = 3600.0

StepAction = Literal[
    "session", "text", "tool_use", "usage", "rate_limit", "write_file", "sleep", "succeed", "fail", "hang", "crash", "raise"
]


class MockStep(BaseModel):
    action: StepAction
    text: str = ""
    path: Optional[str] = None
    content: str = ""
    seconds: float = 0.0
    category: Optional[ErrorCategory] = None
    data: dict[str, Any] = Field(default_factory=dict)
    structured_output: Optional[Any] = None


class MockScript(BaseModel):
    steps: list[MockStep] = Field(default_factory=list)
    model: Optional[str] = None

    @classmethod
    def success(
        cls,
        text: str = "done",
        *,
        files: Optional[dict[str, str]] = None,
        usage: Optional[dict[str, Any]] = None,
        structured_output: Any = None,
    ) -> "MockScript":
        steps = [MockStep(action="text", text=text)]
        for path, content in (files or {}).items():
            steps.append(MockStep(action="write_file", path=path, content=content))
        steps.append(MockStep(action="usage", data=usage or {"input_tokens": 10, "output_tokens": 5}))
        steps.append(MockStep(action="succeed", text=text, structured_output=structured_output))
        return cls(steps=steps)

    @classmethod
    def failure(cls, category: ErrorCategory = ErrorCategory.UNKNOWN, message: str = "mock failure", **data: Any) -> "MockScript":
        steps = []
        if category in (ErrorCategory.RATE_LIMIT, ErrorCategory.QUOTA_EXHAUSTED) and data:
            steps.append(MockStep(action="rate_limit", data=data))
        steps.append(MockStep(action="fail", category=category, text=message))
        return cls(steps=steps)

    @classmethod
    def hang(cls, seconds: float = MAX_HANG_SECONDS) -> "MockScript":
        return cls(steps=[MockStep(action="text", text="working..."), MockStep(action="hang", seconds=seconds)])

    @classmethod
    def crash(cls, message: str = "process exited unexpectedly") -> "MockScript":
        return cls(steps=[MockStep(action="crash", text=message)])


Scripter = Callable[[ExecutionRequest, int], Optional[MockScript]]


class MockProvider(ProviderAdapter):
    def __init__(
        self,
        name: str = "mock",
        *,
        scripts: Optional[list[MockScript]] = None,
        scripter: Optional[Scripter] = None,
        default_script: Optional[MockScript] = None,
        model: str = "mock-model",
    ):
        self.name = name
        self.model = model
        self._queue: list[MockScript] = list(scripts or [])
        self._scripter = scripter
        self._default = default_script or MockScript.success()
        self._cancel: dict[str, asyncio.Event] = {}
        self.requests: list[ExecutionRequest] = []
        self.cancelled: list[str] = []

    # ---- scripting ---------------------------------------------------------
    def queue(self, *scripts: MockScript) -> None:
        self._queue.extend(scripts)

    def _next_script(self, request: ExecutionRequest) -> MockScript:
        index = len(self.requests) - 1
        if self._queue:
            return self._queue.pop(0)
        if self._scripter is not None:
            script = self._scripter(request, index)
            if script is not None:
                return script
        return self._default

    # ---- contract ----------------------------------------------------------
    async def detect(self) -> ProviderStatus:
        return ProviderStatus(
            provider=self.name,
            installed=True,
            version="mock",
            executable=None,
            authenticated=True,
            auth_detail="mock provider (ORCHESTRATOR_ENABLE_MOCK=1)",
            capabilities=ProviderCapabilities(
                structured_stream=True, cancel=True, model_selection=True, usage_tokens=True, task_capabilities=["coding"]
            ),
        )

    async def discover_models(self) -> list[ModelInfo]:
        return [ModelInfo(provider=self.name, model_id=self.model, display_name="Mock model", source="static")]

    def classify_error(self, exit_code: Optional[int], stdout_tail: str, stderr_tail: str) -> ErrorCategory:
        text = f"{stdout_tail}\n{stderr_tail}".lower()
        if "rate limit" in text:
            return ErrorCategory.RATE_LIMIT
        if "quota" in text:
            return ErrorCategory.QUOTA_EXHAUSTED
        return ErrorCategory.UNKNOWN

    async def cancel(self, run_id: str) -> bool:
        event = self._cancel.get(run_id)
        if event is None:
            return False
        self.cancelled.append(run_id)
        event.set()
        return True

    def is_running(self, run_id: str) -> bool:
        return run_id in self._cancel

    async def execute(self, request: ExecutionRequest) -> AsyncIterator[AgentEvent]:  # type: ignore[override]
        self.requests.append(request)
        script = self._next_script(request)
        model = script.model or request.model or self.model
        cancel = asyncio.Event()
        self._cancel[request.run_id] = cancel
        loop = asyncio.get_running_loop()
        started = loop.time()
        usage: dict[str, Any] = {}
        texts: list[str] = []

        def result(success: bool, category: Optional[ErrorCategory] = None, message: str = "", **kw: Any) -> AgentEvent:
            res = ExecutionResult(
                success=success,
                final_text=kw.pop("final_text", "\n".join(texts)),
                session_id=f"mock-session-{request.run_id}",
                model=model,
                exit_code=0 if success else kw.pop("exit_code", 1),
                error_category=category,
                error_message=message or None,
                usage=usage,
                duration_seconds=loop.time() - started,
                **kw,
            )
            return AgentEvent(kind=AgentEventKind.RESULT, data=res.model_dump(mode="json"))

        async def wait_cancel(seconds: float) -> bool:
            try:
                await asyncio.wait_for(cancel.wait(), timeout=max(0.0, min(seconds, MAX_HANG_SECONDS)))
                return True
            except asyncio.TimeoutError:
                return False

        try:
            yield AgentEvent(kind=AgentEventKind.SESSION, data={"session_id": f"mock-session-{request.run_id}", "model": model})
            for step in script.steps:
                if cancel.is_set():
                    yield result(False, ErrorCategory.CANCELLED, "cancelled")
                    return
                a = step.action
                if a == "session":
                    continue
                if a == "text":
                    texts.append(step.text)
                    yield AgentEvent(kind=AgentEventKind.TEXT, data={"text": step.text})
                elif a == "tool_use":
                    yield AgentEvent(kind=AgentEventKind.TOOL_USE, data={"tool": step.data.get("tool", "mock_tool"), "summary": step.text, **step.data})
                elif a == "usage":
                    for k, v in step.data.items():
                        if isinstance(v, (int, float)) and isinstance(usage.get(k, 0), (int, float)):
                            usage[k] = usage.get(k, 0) + v
                        else:
                            usage[k] = v
                    yield AgentEvent(kind=AgentEventKind.USAGE, data=dict(step.data))
                elif a == "rate_limit":
                    yield AgentEvent(kind=AgentEventKind.RATE_LIMIT, data=dict(step.data))
                elif a == "write_file":
                    target = self._safe_target(request.cwd, step.path or "")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(step.content, encoding="utf-8")
                    yield AgentEvent(
                        kind=AgentEventKind.TOOL_USE,
                        data={"tool": "write_file", "summary": f"wrote {step.path}", "files": [step.path]},
                    )
                elif a == "sleep":
                    if await wait_cancel(step.seconds):
                        yield result(False, ErrorCategory.CANCELLED, "cancelled")
                        return
                elif a == "hang":
                    limit = step.seconds or MAX_HANG_SECONDS
                    if request.timeout_seconds:
                        limit = min(limit, request.timeout_seconds)
                    if await wait_cancel(limit):
                        yield result(False, ErrorCategory.CANCELLED, "cancelled")
                    else:
                        yield result(False, ErrorCategory.TIMEOUT, "mock hang timed out")
                    return
                elif a == "crash":
                    yield result(False, ErrorCategory.CRASHED, step.text or "crashed", exit_code=-1)
                    return
                elif a == "raise":
                    raise RuntimeError(step.text or "mock adapter bug")
                elif a == "fail":
                    category = step.category or ErrorCategory.UNKNOWN
                    yield AgentEvent(kind=AgentEventKind.ERROR, data={"category": category.value, "message": step.text})
                    yield result(False, category, step.text or "failed")
                    return
                elif a == "succeed":
                    yield result(True, final_text=step.text or "\n".join(texts), structured_output=step.structured_output)
                    return
            yield result(True)
        finally:
            self._cancel.pop(request.run_id, None)

    @staticmethod
    def _safe_target(cwd: str, rel: str) -> Path:
        if not rel or os.path.isabs(rel):
            raise ValueError("mock write_file needs a relative path")
        base = Path(os.path.realpath(cwd))
        target = Path(os.path.realpath(base / rel))
        if os.path.commonpath([os.path.normcase(str(base)), os.path.normcase(str(target))]) != os.path.normcase(str(base)):
            raise ValueError("mock write_file path escapes cwd")
        return target
