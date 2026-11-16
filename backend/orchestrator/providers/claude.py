"""Claude Code adapter (`claude -p --output-format stream-json --verbose`).

Prompt goes on stdin. Verified event shapes are documented in config/models.yaml
and docs/ARCHITECTURE.md section 7; the parser is tolerant of everything else.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml

from .base import (
    AgentEvent,
    AgentEventKind,
    ErrorCategory,
    ExecutionRequest,
    ModelInfo,
    ProviderCapabilities,
    ProviderStatus,
    UnsupportedOperation,
)
from .process import (
    COMMON_NETWORK,
    CliAdapterBase,
    CommandSpec,
    ParseState,
    loads_object,
    raw_event,
    repo_config_dir,
    run_capture,
)

READ_ONLY_TOOLS = "Read,Glob,Grep"
# workspace_write default: file tools only. With `--restricted` (below) they are confined to the working
# directories (cwd + --add-dir). Bash is added only with the YAML-only policy `worker_permissions.claude_shell`,
# and a shell is NOT confined to the worktree.
WRITE_TOOLS = "Read,Write,Edit,MultiEdit,NotebookEdit,Glob,Grep"
SHELL_TOOL = "Bash"
# Denied on every run as belt and braces (`--disallowedTools`, verified in `claude --help` 2.1.322).
ALWAYS_DENIED_TOOLS = ("PowerShell", "WebFetch", "WebSearch")
# Security flags on EVERY run (verified in `claude --help` 2.1.322 and by the architect with real runs):
#   --restricted: no command/code-running tools unless --tools names them, user/project/local settings files are
#     ignored (repository hooks do not run, finding #4), file tools confined to the working dirs (finding #3);
#   --strict-mcp-config: no MCP servers except --mcp-config (none passed);
#   --disable-slash-commands: no skills/commands.
SECURITY_FLAGS = ("--restricted", "--strict-mcp-config", "--disable-slash-commands")
MAX_SCHEMA_ARG_CHARS = 20_000  # the schema is an argv element; stay far below the Windows 32k limit

# Built-in fallback when config/models.yaml is unavailable. Aliases resolve inside the CLI at run time.
STATIC_ALIASES = [("fable", "frontier"), ("opus", "advanced"), ("sonnet", "standard"), ("haiku", "fast")]

ERROR_TABLE: list[tuple[ErrorCategory, tuple[str, ...]]] = [
    (ErrorCategory.AUTH, (r"not logged in", r"please run /login", r"invalid api key", r"authentication_error",
                          r"oauth token (has )?(expired|revoked)", r"invalid (x-api-key|bearer)", r"\b401\b",
                          r"login (is )?required", r"credentials? (are )?(missing|invalid)")),
    (ErrorCategory.QUOTA_EXHAUSTED, (r"usage limit reached", r"limit reached", r"5-hour limit", r"five[- ]hour limit",
                                     r"weekly limit", r"you'?ve hit your (usage )?limit", r"hit your limit",
                                     r"out of (extra )?usage", r"credit balance is too low", r"usage limit")),
    (ErrorCategory.RATE_LIMIT, (r"rate[_ ]limit", r"\b429\b", r"too many requests", r"overloaded", r"\b529\b")),
    (ErrorCategory.NETWORK, COMMON_NETWORK + (r"api_error.*timeout", r"request timed out")),
    (ErrorCategory.INVALID_REQUEST, (r"unknown option", r"unknown argument", r"unexpected argument", r"invalid model",
                                     r"issue with the selected model", r"model .{0,60}(not found|does not exist)",
                                     r"no conversation found", r"invalid session", r"invalid value for",
                                     r"error: option .* argument missing", r"invalid_request_error",
                                     r"json schema", r"prompt is too long")),
    (ErrorCategory.PERMISSION_DENIED, (r"permission (was )?denied", r"requires (user )?approval",
                                       r"not allowed to use", r"tool use was denied", r"permission_denials")),
]


def claude_rule_paths(path: str | Path) -> list[str]:
    """Absolute path in Claude Code permission-rule syntax: `//` + POSIX path; on Windows `C:\\x` -> `//c/x`.

    The `//absolute` form and the Windows drive normalisation follow the Claude Code permissions documentation
    as recalled, not something `claude --help` states; `--restricted` remains the primary control. Both drive
    letter cases are emitted because the documentation does not state whether matching is case-sensitive."""
    p = os.path.normpath(os.path.realpath(os.fspath(path)))
    if re.match(r"^[A-Za-z]:", p):
        rest = p[2:].replace("\\", "/").rstrip("/")
        return list(dict.fromkeys([f"//{p[0].lower()}{rest}", f"//{p[0].upper()}{rest}"]))
    return ["/" + p.replace("\\", "/").rstrip("/")]


def _model_table() -> dict[str, dict[str, Any]]:
    try:
        data = yaml.safe_load((repo_config_dir() / "models.yaml").read_text(encoding="utf-8"))
        return {m["alias"]: m for m in data["providers"]["claude"]["models"] if "alias" in m}
    except Exception:  # noqa: BLE001
        return {}


def _tool_files(inp: Any) -> list[str]:
    if not isinstance(inp, dict):
        return []
    files = [v for k in ("file_path", "path", "notebook_path") if isinstance((v := inp.get(k)), str) and v]
    edits = inp.get("edits")
    if isinstance(edits, list):
        files += [e["file_path"] for e in edits if isinstance(e, dict) and isinstance(e.get("file_path"), str)]
    return list(dict.fromkeys(files))


def _tool_summary(name: str, inp: Any) -> str:
    if not isinstance(inp, dict):
        return name
    for key in ("command", "pattern", "description", "file_path", "path", "url", "query"):
        v = inp.get(key)
        if isinstance(v, str) and v:
            return f"{name}: {v[:200]}"
    return name


def _utilization(info: dict[str, Any]) -> tuple[float | None, str | None, Any | None]:
    """(utilization, window name, resets_at) preferring the most-utilised unified window."""
    windows = info.get("unifiedWindows")
    util: float | None = None
    window = info.get("rateLimitType")
    resets = info.get("resetsAt")
    if isinstance(windows, dict):
        for wname, w in windows.items():
            if isinstance(w, dict) and isinstance(w.get("utilization"), (int, float)):
                if util is None or w["utilization"] > util:
                    util, window, resets = float(w["utilization"]), wname, w.get("resetsAt", resets)
    return util, window, resets


def parse_claude_line(line: str, state: ParseState) -> list[AgentEvent]:
    """Pure mapping of one stream-json line to AgentEvents (state is the accumulator)."""
    obj = loads_object(line)
    if obj is None:
        return [raw_event(line, "non-json")] if line.strip() else []
    typ = obj.get("type")
    events: list[AgentEvent] = []

    if typ == "system":
        if obj.get("subtype") == "init":
            state.session_id = obj.get("session_id") or state.session_id
            state.model = obj.get("model") or state.model
            events.append(AgentEvent(kind=AgentEventKind.SESSION,
                                     data={"session_id": state.session_id, "model": state.model}))
        else:
            events.append(raw_event(line, f"system/{obj.get('subtype')}"))
    elif typ == "assistant":
        msg = obj.get("message")
        if not isinstance(msg, dict):
            return [raw_event(line, "assistant without message")]
        state.model = msg.get("model") or state.model
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" and isinstance(block.get("text"), str):
                state.texts.append(block["text"])
                events.append(AgentEvent(kind=AgentEventKind.TEXT, data={"text": block["text"]}))
            elif btype == "tool_use":
                name = str(block.get("name") or "unknown")
                inp = block.get("input")
                data: dict[str, Any] = {"tool": name, "summary": _tool_summary(name, inp)}
                files = _tool_files(inp)
                if files:
                    data["files"] = files
                events.append(AgentEvent(kind=AgentEventKind.TOOL_USE, data=data))
            # thinking / redacted_thinking blocks are intentionally dropped
    elif typ == "user":
        # tool results: large and rarely useful; only note denials so permission failures are attributable
        content = (obj.get("message") or {}).get("content") if isinstance(obj.get("message"), dict) else None
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                    body = json.dumps(b.get("content"))[:300].lower()
                    if "permission" in body or "denied" in body or "requires approval" in body:
                        state.permission_denials += 1
    elif typ == "rate_limit_event":
        info = obj.get("rate_limit_info")
        if isinstance(info, dict):
            util, window, resets = _utilization(info)
            data = {"status": info.get("status"), "utilization": util, "resets_at": resets, "window": window,
                    "overage_status": info.get("overageStatus"), "is_using_overage": info.get("isUsingOverage")}
            state.rate_limit = data
            events.append(AgentEvent(kind=AgentEventKind.RATE_LIMIT, data=data))
        else:
            events.append(raw_event(line, "rate_limit_event without info"))
    elif typ == "result":
        usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
        udata: dict[str, Any] = {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cached_input_tokens": usage.get("cache_read_input_tokens"),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        }
        if isinstance(obj.get("total_cost_usd"), (int, float)):
            udata["cost_usd"] = obj["total_cost_usd"]
            udata["cost_basis"] = "list_price_equivalent"  # subscription login: not money actually charged
        udata = {k: v for k, v in udata.items() if v is not None}
        state.usage = udata
        if isinstance(obj.get("num_turns"), int):
            state.usage["num_turns"] = obj["num_turns"]
        model_usage = obj.get("modelUsage")
        if isinstance(model_usage, dict) and model_usage and not state.model:
            state.model = next(iter(model_usage))
        if udata:
            events.append(AgentEvent(kind=AgentEventKind.USAGE, data=udata))
        is_error = bool(obj.get("is_error")) or obj.get("subtype") not in (None, "success")
        state.success = not is_error
        if isinstance(obj.get("result"), str):
            state.final_text = obj["result"]
        if obj.get("structured_output") is not None:
            state.structured_output = obj["structured_output"]
        denials = obj.get("permission_denials")
        if isinstance(denials, list) and denials:
            state.permission_denials += len(denials)
        if is_error:
            msg = state.final_text or f"claude reported {obj.get('subtype') or 'error'}"
            if obj.get("api_error_status"):
                msg = f"{msg} (api_error_status={obj['api_error_status']})"
            state.error_messages.append(msg)
    else:
        events.append(raw_event(line, f"unknown type {typ!r}"))
    return events


class ClaudeAdapter(CliAdapterBase):
    name = "claude"
    cli_name = "claude"

    def __init__(self, *, persist_sessions: bool = True, task_capabilities: list[str] | None = None,
                 allow_shell: bool | None = None, **kw: Any):
        super().__init__(**kw)
        self.persist_sessions = persist_sessions  # False adds --no-session-persistence (disables resume)
        self.task_capabilities = task_capabilities or []
        # None = read `worker_permissions.claude_shell` from config/policies.yaml (never from the settings table).
        self._allow_shell = allow_shell

    @property
    def allow_shell(self) -> bool:
        if self._allow_shell is not None:
            return self._allow_shell
        from orchestrator.config import yaml_claude_shell

        try:
            return yaml_claude_shell()
        except Exception:  # noqa: BLE001 - unreadable policy file: fail closed (no shell)
            return False

    def write_tools(self) -> str:
        return f"{WRITE_TOOLS},{SHELL_TOOL}" if self.allow_shell else WRITE_TOOLS

    def deny_settings(self) -> str:
        """`--settings` JSON with permissions.deny rules for the data dir (defence in depth behind --restricted)."""
        rules: list[str] = []
        for d in self.protected_dirs():
            for base in claude_rule_paths(d):
                for tool in ("Read", "Edit"):
                    rules += [f"{tool}({base})", f"{tool}({base}/**)"]
        return json.dumps({"permissions": {"deny": list(dict.fromkeys(rules))}}, separators=(",", ":"))

    # ---- command
    def build_command(self, request: ExecutionRequest) -> CommandSpec:
        self.check_request(request)  # session id + cwd/extra dirs vs the data dir (also done in execute())
        args = ["-p", "--output-format", "stream-json", "--verbose", *SECURITY_FLAGS]
        if not self.persist_sessions:
            args.append("--no-session-persistence")
        denied = list(ALWAYS_DENIED_TOOLS)
        if request.permission == "read_only":
            args += ["--permission-mode", "plan", "--tools", READ_ONLY_TOOLS, "--allowedTools", READ_ONLY_TOOLS]
            denied.insert(0, SHELL_TOOL)
        elif request.permission == "workspace_write":
            tools = self.write_tools()
            args += ["--permission-mode", "acceptEdits", "--permission-prompts", "none",
                     "--tools", tools, "--allowedTools", tools]
            if not self.allow_shell:
                denied.insert(0, SHELL_TOOL)
        else:  # pragma: no cover - Literal guards this
            raise UnsupportedOperation(f"permission profile {request.permission!r}")
        args += ["--disallowedTools", ",".join(denied), "--settings", self.deny_settings()]
        if request.model:
            args += ["--model", request.model]
        if request.resume_session_id:
            # attached form: commander (see `--debug=hooks,mcp` in --help) never reads the value as an option
            args.append(f"--resume={request.resume_session_id}")
        for d in request.extra_dirs:
            args += ["--add-dir", d]
        if request.json_schema is not None:
            schema = json.dumps(request.json_schema, separators=(",", ":"))
            if len(schema) > MAX_SCHEMA_ARG_CHARS:
                raise UnsupportedOperation("json_schema too large to pass on the command line")
            args += ["--json-schema", schema]
        return CommandSpec(args=args, stdin_text=request.prompt)

    def parse_line(self, line: str, state: ParseState) -> list[AgentEvent]:
        return parse_claude_line(line, state)

    def error_table(self) -> list[tuple[ErrorCategory, tuple[str, ...]]]:
        return ERROR_TABLE

    def finalize_state(self, state: ParseState, request: ExecutionRequest) -> None:
        if request.json_schema is not None and state.structured_output is None and state.success:
            try:
                state.structured_output = json.loads(state.text())
            except ValueError:
                pass

    # ---- detection
    async def detect(self) -> ProviderStatus:
        caps = ProviderCapabilities(
            structured_stream=True, resume=True, cancel=True, model_selection=True, model_discovery=False,
            usage_tokens=True, usage_cost=True, rate_limit_info=True, json_schema_output=True,
            sandbox_modes=["read_only", "workspace_write"], task_capabilities=self.task_capabilities,
        )
        try:
            exe = self.resolve()
        except Exception as exc:  # noqa: BLE001
            return ProviderStatus(provider=self.name, installed=False, capabilities=caps, error=str(exc))
        env = self.child_env()
        code, out, err = await run_capture([*exe.argv_prefix, "--version"], env=env)
        version = None
        if code == 0:
            m = re.search(r"\d+\.\d+\.\d+", out)
            version = m.group(0) if m else out.strip()[:40]
        status = ProviderStatus(provider=self.name, installed=code == 0, version=version, executable=exe.path,
                                capabilities=caps, error=None if code == 0 else (err.strip()[:300] or "version check failed"))
        if code != 0:
            return status
        code, out, err = await run_capture([*exe.argv_prefix, "auth", "status"], env=env)
        try:
            info = json.loads(out)
            logged = info.get("loggedIn")
            if isinstance(logged, bool):
                status.authenticated = logged
                parts = [str(info.get("authMethod") or "unknown method")]
                if info.get("subscriptionType"):
                    parts.append(f"plan {info['subscriptionType']}")
                status.auth_detail = ", ".join(parts)  # email/org fields are deliberately never read
        except (ValueError, AttributeError):
            status.auth_detail = "auth status unavailable"
        return status

    async def discover_models(self) -> list[ModelInfo]:
        table = _model_table()
        caps = self.task_capabilities
        if table:
            return [ModelInfo(provider=self.name, model_id=alias, display_name=f"{alias} ({m.get('id', alias)})",
                              source="config", tier=m.get("tier", "standard"), capabilities=list(caps))
                    for alias, m in table.items()]
        return [ModelInfo(provider=self.name, model_id=a, display_name=a, source="static", tier=t,  # type: ignore[arg-type]
                          capabilities=list(caps)) for a, t in STATIC_ALIASES]
