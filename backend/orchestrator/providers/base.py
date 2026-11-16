"""Provider adapter contract. Binding for every adapter (see docs/ARCHITECTURE.md §7).

Adapters wrap an installed CLI. They never invent capabilities: anything the
CLI does not expose raises UnsupportedOperation and is absent from
ProviderCapabilities.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, AsyncIterator, Literal, Optional

from pydantic import BaseModel, Field


class UnsupportedOperation(Exception):
    """The underlying CLI does not support this operation."""


class RecursionGuardError(Exception):
    """Refused to start a nested orchestrator-launched agent."""


class ErrorCategory(str, Enum):
    RATE_LIMIT = "rate_limit"  # temporary throttle; retry after cooldown
    QUOTA_EXHAUSTED = "quota_exhausted"  # plan/usage limit reached until reset
    AUTH = "auth"
    NETWORK = "network"
    TIMEOUT = "timeout"
    INVALID_REQUEST = "invalid_request"  # bad flag/model/prompt; retrying as-is is pointless
    TOOL_FAILURE = "tool_failure"
    PERMISSION_DENIED = "permission_denied"  # agent needed a permission it was not granted
    CANCELLED = "cancelled"
    CRASHED = "crashed"
    UNKNOWN = "unknown"


class ProviderCapabilities(BaseModel):
    structured_stream: bool = False
    resume: bool = False
    cancel: bool = True
    model_selection: bool = False
    model_discovery: bool = False
    usage_tokens: bool = False
    usage_cost: bool = False
    rate_limit_info: bool = False
    json_schema_output: bool = False
    sandbox_modes: list[str] = Field(default_factory=list)
    # Tool-level capabilities actually exposed by the installed CLI.
    # Values come from ARCHITECTURE §9 capability flags (e.g. "image_generation").
    task_capabilities: list[str] = Field(default_factory=list)


class ProviderStatus(BaseModel):
    provider: str
    installed: bool
    version: Optional[str] = None
    executable: Optional[str] = None
    authenticated: Optional[bool] = None  # None = could not be determined
    auth_detail: Optional[str] = None  # never contains secrets
    capabilities: ProviderCapabilities = Field(default_factory=ProviderCapabilities)
    error: Optional[str] = None


class ModelInfo(BaseModel):
    provider: str
    model_id: str
    display_name: Optional[str] = None
    source: Literal["cli", "config", "static"] = "static"
    tier: Literal["fast", "standard", "advanced", "frontier"] = "standard"
    capabilities: list[str] = Field(default_factory=list)


PermissionProfile = Literal["read_only", "workspace_write"]


class ExecutionRequest(BaseModel):
    run_id: str
    prompt: str
    cwd: str  # must already be validated by security.paths
    model: Optional[str] = None
    permission: PermissionProfile = "workspace_write"
    resume_session_id: Optional[str] = None
    json_schema: Optional[dict[str, Any]] = None  # only if capabilities.json_schema_output
    timeout_seconds: Optional[float] = None
    extra_dirs: list[str] = Field(default_factory=list)


class AgentEventKind(str, Enum):
    SESSION = "session"  # data: {session_id, model}
    TEXT = "text"  # data: {text} (delta or full message)
    TOOL_USE = "tool_use"  # data: {tool, summary, files?}
    USAGE = "usage"  # data: {input_tokens, output_tokens, cached_input_tokens?, cost_usd?}
    RATE_LIMIT = "rate_limit"  # data: {status, utilization?, resets_at?, window?}
    ERROR = "error"  # data: {category, message}
    RESULT = "result"  # data: ExecutionResult.model_dump()
    RAW = "raw"  # unparsed provider event, kept for troubleshooting


class AgentEvent(BaseModel):
    kind: AgentEventKind
    data: dict[str, Any] = Field(default_factory=dict)


class ExecutionResult(BaseModel):
    success: bool
    final_text: str = ""
    structured_output: Optional[Any] = None
    session_id: Optional[str] = None
    model: Optional[str] = None
    exit_code: Optional[int] = None
    error_category: Optional[ErrorCategory] = None
    error_message: Optional[str] = None
    usage: dict[str, Any] = Field(default_factory=dict)  # measured values only
    duration_seconds: Optional[float] = None


class ProviderAdapter(ABC):
    """One instance per provider. Stateless apart from live process handles."""

    name: str

    @abstractmethod
    async def detect(self) -> ProviderStatus:
        """Check installation, version, auth. Must not spend model quota."""

    @abstractmethod
    async def discover_models(self) -> list[ModelInfo]:
        """Return models from the CLI where possible; mark source honestly."""

    @abstractmethod
    def execute(self, request: ExecutionRequest) -> AsyncIterator[AgentEvent]:
        """Run one prompt. Yields events; the last event is always RESULT.

        Must: use arg arrays, send the prompt via stdin/temp file, enforce the
        recursion guard, apply request.permission, and never raise for provider
        failures (they become a RESULT with success=False and an error_category).
        """

    @abstractmethod
    async def cancel(self, run_id: str) -> bool:
        """Terminate the process tree for run_id. True if something was killed."""

    @abstractmethod
    def classify_error(self, exit_code: Optional[int], stdout_tail: str, stderr_tail: str) -> ErrorCategory:
        """Provider-specific error classification."""

    def pid_for(self, run_id: str) -> Optional[int]:
        return None
