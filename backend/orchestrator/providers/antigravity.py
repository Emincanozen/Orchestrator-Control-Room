"""Antigravity (`agy`) adapter.

Prompt delivery: `agy -p=` combined with `--input-format stream-json` reads NDJSON
messages from stdin. The accepted user message shape was established by probing
the CLI's own validation errors (no model call):
    {"event":"user","message":{"content":"<prompt>"}}
so the prompt never appears in argv.
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

MODE = {"read_only": "plan", "workspace_write": "accept-edits"}
MAX_SCHEMA_ARG_CHARS = 20_000

ERROR_TABLE: list[tuple[ErrorCategory, tuple[str, ...]]] = [
    (ErrorCategory.AUTH, (r"unauthenticated", r"not (signed|logged) in", r"sign in", r"login required",
                          r"\b401\b", r"authentication (failed|required)", r"invalid credentials")),
    (ErrorCategory.RATE_LIMIT, (r"rate[_ ]limit", r"too many requests", r"\b429\b")),
    (ErrorCategory.QUOTA_EXHAUSTED, (r"resource[_ ]?exhausted", r"quota", r"exhausted your capacity", r"usage limit",
                                     r"limit reached")),
    (ErrorCategory.NETWORK, COMMON_NETWORK + (r"deadline exceeded", r"\bunavailable\b.*(connect|dial)")),
    (ErrorCategory.INVALID_REQUEST, (r"flag provided but not defined", r"unknown (flag|model|command)",
                                     r"invalid (model|value|mode)", r"model .{0,60}not found", r"empty prompt",
                                     r"unsupported stream input", r"failed to decode stream input",
                                     r"conversation .{0,60}not found", r"took .{0,30} as its prompt")),
    (ErrorCategory.PERMISSION_DENIED, (r"permission (was )?denied", r"user rejected", r"request-review")),
]

_TOOLISH = re.compile(r"tool|command|file|edit|write|browser|search|grep|read|run_|mcp|view|list_|find|replace|sed|open_|click|capture|execute|notebook", re.IGNORECASE)


def _step_files(step: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for k in ("file_path", "path", "target_file", "absolute_path", "file"):
        v = step.get(k)
        if isinstance(v, str) and v:
            out.append(v)
    for k in ("files", "paths"):
        v = step.get(k)
        if isinstance(v, list):
            out += [x for x in v if isinstance(x, str)]
    # Verified (agy 1.5.6): file tools carry the path in tool_info.parameters.Path (read_file, write_file).
    info = step.get("tool_info")
    params = info.get("parameters") if isinstance(info, dict) else None
    if isinstance(params, dict):
        for k in ("Path", "path", "TargetFile", "AbsolutePath", "file_path"):
            v = params.get(k)
            if isinstance(v, str) and v:
                out.append(v)
    return list(dict.fromkeys(out))


def _usage(u: Any) -> dict[str, Any]:
    if not isinstance(u, dict):
        return {}
    mapping = {"input_tokens": "input_tokens", "output_tokens": "output_tokens",
               "cache_read_tokens": "cached_input_tokens", "thinking_tokens": "reasoning_output_tokens",
               "total_tokens": "total_tokens"}
    return {dst: u[src] for src, dst in mapping.items() if isinstance(u.get(src), (int, float))}


def parse_agy_line(line: str, state: ParseState) -> list[AgentEvent]:
    obj = loads_object(line)
    if obj is None:
        # agy prints "error: ..." warnings on stderr and sometimes stdout; keep them for classification
        if line.strip().lower().startswith("error"):
            state.error_messages.append(line.strip())
        return [raw_event(line, "non-json")] if line.strip() else []
    ev = obj.get("event")
    events: list[AgentEvent] = []
    if ev == "init":
        init = obj.get("init") if isinstance(obj.get("init"), dict) else {}
        state.session_id = obj.get("conversation_id") or state.session_id
        state.model = init.get("model") or state.model
        events.append(AgentEvent(kind=AgentEventKind.SESSION,
                                 data={"session_id": state.session_id, "model": state.model}))
    elif ev == "step_update":
        step = obj.get("step_update")
        if not isinstance(step, dict):
            return [raw_event(line, "step_update without body")]
        stype = str(step.get("step_type") or "")
        if stype == "agent_response":
            delta = step.get("text_delta")
            if isinstance(delta, str) and delta:
                state.texts.append(delta)
                events.append(AgentEvent(kind=AgentEventKind.TEXT, data={"text": delta}))
        elif stype and _TOOLISH.search(stype):
            key = (step.get("conversation_id"), step.get("step_index"))
            if key not in state.seen_steps:
                state.seen_steps.add(key)
                # Verified shape (agy 1.5.6): step_type "tool" with tool_name and tool_info{name, parameters}.
                info = step.get("tool_info") if isinstance(step.get("tool_info"), dict) else {}
                tool = str(step.get("tool_name") or info.get("name") or stype)
                params = info.get("parameters") if isinstance(info.get("parameters"), dict) else {}
                summary = tool
                if params:
                    summary = f"{tool} " + json.dumps(params, ensure_ascii=False)[:300]
                data: dict[str, Any] = {"tool": tool, "summary": summary}
                files = _step_files(step)
                if files:
                    data["files"] = files
                events.append(AgentEvent(kind=AgentEventKind.TOOL_USE, data=data))
        else:
            events.append(raw_event(line, f"step_type {stype!r}"))
    elif ev == "result":
        res = obj.get("result") if isinstance(obj.get("result"), dict) else {}
        usage = _usage(res.get("usage"))
        state.usage = usage
        if usage:
            events.append(AgentEvent(kind=AgentEventKind.USAGE, data=dict(usage)))
        status = str(res.get("status") or "").upper()
        state.success = status == "SUCCESS"
        if isinstance(res.get("response"), str):
            state.final_text = res["response"].strip("\n") if state.success else res["response"]
        if res.get("conversation_id"):
            state.session_id = res["conversation_id"]
        # Verified (agy 1.5.6): headless mode auto-denies tools it cannot prompt for and still reports
        # status SUCCESS with an empty response plus "denied_actions". That is not a success.
        denied = res.get("denied_actions")
        if state.success and isinstance(denied, list) and denied:
            names = ", ".join(str(d.get("display_name") or d.get("action") or "?") for d in denied if isinstance(d, dict))
            state.success = False
            state.error_messages.append(f"permission denied: agy auto-denied tool actions in headless mode ({names})")
        elif not state.success:
            state.error_messages.append(str(res.get("error") or f"agy finished with status {status or 'unknown'}"))
    else:
        events.append(raw_event(line, f"unknown event {ev!r}"))
    return events


class AntigravityAdapter(CliAdapterBase):
    name = "antigravity"
    cli_name = "agy"
    extra_executable_candidates = (
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "bin" / "agy.exe") if os.environ.get("LOCALAPPDATA") else "",
    )

    def __init__(self, *, task_capabilities: list[str] | None = None, **kw: Any):
        super().__init__(**kw)
        self.task_capabilities = task_capabilities or []

    def build_command(self, request: ExecutionRequest) -> CommandSpec:
        self.check_request(request)  # session id + cwd/extra dirs vs the data dir (also done in execute())
        # "-p=" (empty attached value) + stream-json input: the prompt is delivered as an NDJSON message on stdin.
        # --disable-slash-commands (in `agy --help` 1.5.6): no slash-command / skill expansion of the prompt.
        args = ["-p=", "--input-format", "stream-json", "--output-format", "stream-json", "--mode", MODE[request.permission],
                "--disable-slash-commands"]
        if request.model:
            args += ["--model", request.model]
        if request.resume_session_id:
            # attached form (Go flag syntax, as `-p=` already relies on): the id can never be read as a flag
            args.append(f"--conversation={request.resume_session_id}")
        for d in request.extra_dirs:
            args += ["--add-dir", d]
        if request.json_schema is not None:
            schema = json.dumps(request.json_schema, separators=(",", ":"))
            if len(schema) > MAX_SCHEMA_ARG_CHARS:
                from .base import UnsupportedOperation
                raise UnsupportedOperation("json_schema too large to pass on the command line")
            args += ["--json-schema", schema]
        message = json.dumps({"event": "user", "message": {"content": request.prompt}}, ensure_ascii=False)
        return CommandSpec(args=args, stdin_text=message + "\n")

    def parse_line(self, line: str, state: ParseState) -> list[AgentEvent]:
        return parse_agy_line(line, state)

    def error_table(self) -> list[tuple[ErrorCategory, tuple[str, ...]]]:
        return ERROR_TABLE

    def finalize_state(self, state: ParseState, request: ExecutionRequest) -> None:
        if request.json_schema is not None and state.success:
            try:
                state.structured_output = json.loads(state.text())
            except ValueError:
                pass

    async def detect(self) -> ProviderStatus:
        caps = ProviderCapabilities(
            structured_stream=True, resume=True, cancel=True, model_selection=True, model_discovery=True,
            usage_tokens=True, usage_cost=False, rate_limit_info=False, json_schema_output=True,
            sandbox_modes=["read_only", "workspace_write"], task_capabilities=self.task_capabilities,
        )
        try:
            exe = self.resolve()
        except Exception as exc:  # noqa: BLE001
            return ProviderStatus(provider=self.name, installed=False, capabilities=caps, error=str(exc))
        code, out, err = await run_capture([*exe.argv_prefix, "--version"], env=self.child_env())
        m = re.search(r"\d+\.\d+\.\d+", out + err)
        return ProviderStatus(
            provider=self.name, installed=code == 0, version=m.group(0) if m else None, executable=exe.path,
            capabilities=caps, authenticated=None,
            auth_detail="agy has no auth-status command; authentication is only proven by a model call",
            error=None if code == 0 else (err.strip()[:300] or "version check failed"),
        )

    def _seed(self) -> dict[str, dict[str, Any]]:
        try:
            data = yaml.safe_load((repo_config_dir() / "models.yaml").read_text(encoding="utf-8"))
            return {m["id"]: m for m in data["providers"]["antigravity"]["models"]}
        except Exception:  # noqa: BLE001
            return {}

    async def discover_models(self) -> list[ModelInfo]:
        seed = self._seed()
        caps = list(self.task_capabilities)
        try:
            exe = self.resolve()
            code, out, _ = await run_capture([*exe.argv_prefix, "models"], env=self.child_env(), timeout=45)
        except Exception:  # noqa: BLE001
            code, out = None, ""
        if code == 0:
            models = parse_agy_models(out, seed, caps)
            if models:
                return models
        return [ModelInfo(provider=self.name, model_id=mid, display_name=mid, source="config",
                          tier=m.get("tier", "standard"), capabilities=caps) for mid, m in seed.items()]


def parse_agy_models(output: str, seed: dict[str, dict[str, Any]], caps: list[str]) -> list[ModelInfo]:
    """Parse `agy models` output: `id<TAB>Display Name` lines after a 'Fetching...' line."""
    models: list[ModelInfo] = []
    for raw in output.splitlines():
        if "\t" not in raw:
            continue
        mid, _, display = raw.partition("\t")
        mid = mid.strip()
        if not mid or " " in mid:
            continue
        models.append(ModelInfo(provider="antigravity", model_id=mid, display_name=display.strip() or mid,
                                source="cli", tier=seed.get(mid, {}).get("tier", "standard"), capabilities=list(caps)))
    return models
