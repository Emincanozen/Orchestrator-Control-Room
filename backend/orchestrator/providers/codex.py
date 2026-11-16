"""Codex CLI adapter (`codex exec --json`, prompt on stdin)."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import logging
import os
import re
import tempfile
import tomllib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import yaml

from orchestrator.security import codex_activation, worker_job
from orchestrator.security.codex_auth import CodexAuthUnavailable, read_subscription_auth
from orchestrator.security.codex_runtime import CodexRunContext, CodexRuntimeCancelled, CodexRuntimeUnavailable
from orchestrator.security.redact import redact

from .base import (
    AgentEvent,
    AgentEventKind,
    ErrorCategory,
    ExecutionRequest,
    ExecutionResult,
    ModelInfo,
    ProviderCapabilities,
    ProviderStatus,
    UnsupportedOperation,
)
from .process import (
    COMMON_NETWORK,
    CliAdapterBase,
    CommandSpec,
    ExecutableResolutionError,
    ParseState,
    check_recursion_guard,
    loads_object,
    raw_event,
    repo_config_dir,
    run_capture,
)

log = logging.getLogger(__name__)


async def _owned_native(context, operation, *args, failure_wins=False):
    """Join native work before propagating even repeated caller cancellation.

    A plain executor future, not an asyncio Task: loop teardown cancels every Task directly and would end
    this join while the native operation is still running (see scheduler/verification._native)."""
    task = asyncio.get_running_loop().run_in_executor(None, contextvars.copy_context().run, operation, *args)
    cancelled = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = exc
            context.cancel_pending_start()
        except Exception:  # noqa: BLE001 - retrieve the joined thread's exact outcome below
            break
    try:
        result = task.result()
    except BaseException:
        if cancelled is not None and not failure_wins:
            raise cancelled
        raise
    if cancelled is not None:
        raise cancelled
    return result


def _prepare_owned(context, environment):
    snapshot = b""
    try:
        snapshot = read_subscription_auth(environment)
        context.prepare(snapshot)
    finally:
        snapshot = b""
        environment = None

SANDBOX = {"read_only": "read-only", "workspace_write": "workspace-write"}
# `isolate_user_tools` (verified from `codex exec --help`, `codex features list` and `codex mcp list --json` with the same
# overrides, 2026-11-12; no model call): `--disable plugins` removes every plugin-provided MCP server, and
# `-c mcp_servers.<name>.enabled=false` disables a server defined in the user's config.toml. Neither touches
# ~/.codex or the auth (login uses CODEX_HOME/auth.json, which is not read through these keys).
ISOLATE_DISABLED_FEATURES = ("plugins", "apps")
# Repository-controlled configuration (finding #4), on every run regardless of isolate_user_tools. From
# `codex exec --help` / `codex exec resume --help` 0.167.1 and `codex features list` (no model call):
#   --ignore-rules   "Do not load user or project execpolicy `.rules` files" (a repository cannot allow commands);
#   --disable hooks  feature flag `hooks` (stable) off: no hook from any config layer runs.
# Not available as a flag: ignoring a project-level `.codex/config.toml` (only --ignore-user-config exists).
SECURITY_ARGS = ("--ignore-rules", "--disable", "hooks")
# The `--output-schema` file (re-review N8) lives in `<data dir>/tmp` (owner-only, not writable by sandboxed
# Codex tool commands, unlike %TEMP%). The Codex CLI process itself is not sandboxed and reads the file as the
# user. Fallback, if a real run shows the CLI cannot read it: ORCHESTRATOR_CODEX_SCHEMA_IN_TEMP=1 restores %TEMP%.
SCHEMA_IN_TEMP_ENV = "ORCHESTRATOR_CODEX_SCHEMA_IN_TEMP"

ERROR_TABLE: list[tuple[ErrorCategory, tuple[str, ...]]] = [
    (ErrorCategory.QUOTA_EXHAUSTED, (r"you'?ve hit your usage limit", r"usage limit", r"upgrade to (plus|pro)",
                                     r"insufficient[_ ]quota", r"quota (exceeded|exhausted)")),
    (ErrorCategory.AUTH, (r"not logged in", r"codex login", r"\b401\b", r"unauthorized", r"refresh token",
                          r"sign in again", r"invalid (api key|token)", r"authentication (failed|required)")),
    (ErrorCategory.RATE_LIMIT, (r"rate[_ ]limit", r"\b429\b", r"too many requests", r"server is overloaded")),
    (ErrorCategory.NETWORK, COMMON_NETWORK + (r"stream disconnected", r"error sending request")),
    (ErrorCategory.INVALID_REQUEST, (r"unknown model", r"model_not_found", r"model .{0,60}(does not exist|not found|not supported)",
                                     r"unexpected argument", r"unrecognized (option|subcommand)", r"invalid value",
                                     r"no (session|conversation|rollout) found", r"invalid (schema|json schema)",
                                     r"not inside a trusted directory")),
    (ErrorCategory.PERMISSION_DENIED, (r"approval (is )?required", r"blocked by (policy|sandbox)", r"sandbox denied",
                                       r"write access .{0,40}denied")),
]


def _files_from_changes(changes: Any) -> list[str]:
    out: list[str] = []
    if isinstance(changes, list):
        for c in changes:
            if isinstance(c, dict) and isinstance(c.get("path"), str):
                out.append(c["path"])
    return out


def parse_codex_line(line: str, state: ParseState) -> list[AgentEvent]:
    obj = loads_object(line)
    if obj is None:
        return [raw_event(line, "non-json")] if line.strip() else []
    typ = obj.get("type")
    events: list[AgentEvent] = []

    if typ == "thread.started":
        state.session_id = obj.get("thread_id") or state.session_id
        events.append(AgentEvent(kind=AgentEventKind.SESSION,
                                 data={"session_id": state.session_id, "model": state.model}))
    elif typ == "turn.started":
        pass
    elif typ in ("item.completed", "item.started", "item.updated"):
        item = obj.get("item")
        if not isinstance(item, dict):
            return [raw_event(line, "item without body")]
        itype = item.get("type")
        if typ != "item.completed":
            # started/updated duplicate the completed event for text items; surface only tool starts once
            if itype in ("command_execution", "mcp_tool_call", "web_search", "file_change") and typ == "item.started":
                key = item.get("id")
                identity = ("started", key) if isinstance(key, str) else None
                if identity is None or identity not in state.seen_steps:
                    if identity is not None:
                        state.seen_steps.add(identity)
                    events.append(_tool_event(item, "started"))
            return events
        if itype == "agent_message":
            text = item.get("text")
            if isinstance(text, str):
                state.texts.append(text)
                state.final_text = text  # the last agent message is the final answer
                events.append(AgentEvent(kind=AgentEventKind.TEXT, data={"text": text}))
        elif itype in ("command_execution", "mcp_tool_call", "web_search", "file_change"):
            key = item.get("id")
            identity = ("completed", key) if isinstance(key, str) else None
            if identity is None or identity not in state.seen_steps:
                if identity is not None:
                    state.seen_steps.add(identity)
                events.append(_tool_event(item, "completed"))
                if item.get("status") == "declined":
                    state.permission_denials += 1
        elif itype == "error":
            msg = str(item.get("message") or "")
            if msg:
                state.error_messages.append(msg)  # item-level errors are often warnings; kept for classification only
            events.append(raw_event(line, "item error"))
        elif itype in ("reasoning", "todo_list"):
            pass
        else:
            events.append(raw_event(line, f"unknown item type {itype!r}"))
    elif typ == "turn.completed":
        usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
        udata = {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cached_input_tokens": usage.get("cached_input_tokens"),
            "reasoning_output_tokens": usage.get("reasoning_output_tokens"),
        }
        udata = {k: v for k, v in udata.items() if v is not None}
        state.usage = udata
        if udata:
            events.append(AgentEvent(kind=AgentEventKind.USAGE, data=udata))
        if state.success is None:
            state.success = True
    elif typ in ("turn.failed", "error"):
        err = obj.get("error")
        msg = (err.get("message") if isinstance(err, dict) else err) or obj.get("message") or "codex reported an error"
        state.error_messages.append(str(msg))
        if typ == "turn.failed":
            state.success = False
        events.append(raw_event(line, typ))
    else:
        events.append(raw_event(line, f"unknown type {typ!r}"))
    return events


def _tool_event(item: dict[str, Any], stage: str) -> AgentEvent:
    itype = item.get("type")
    data: dict[str, Any]
    if itype == "command_execution":
        data = {"tool": "shell", "summary": "shell: " + redact(str(item.get('command') or ''))[:200]}
    elif itype == "mcp_tool_call":
        tool = f"{item.get('server') or 'mcp'}.{item.get('tool') or 'tool'}"
        data = {"tool": redact(tool), "summary": redact(tool)}
    elif itype == "file_change":
        files = _files_from_changes(item.get("changes"))
        data = {"tool": "file_change", "summary": "file_change: " + redact(', '.join(files))[:200]}
        if files:
            data["files"] = files
    else:
        data = {"tool": "web_search", "summary": "web_search: " + redact(str(item.get('query') or ''))[:200]}
    data["stage"] = stage
    if isinstance(item.get("id"), str):
        data["tool_call_id"] = item["id"]
    if isinstance(item.get("status"), str):
        data["status"] = item["status"][:64]
    if type(item.get("exit_code")) is int:
        data["exit_code"] = item["exit_code"]
    nonzero_exit = type(item.get("exit_code")) is int and item["exit_code"] != 0
    if stage == "completed" and (item.get("status") in ("failed", "declined") or nonzero_exit):
        error = item.get("error")
        diagnostic = error.get("message") if isinstance(error, dict) else error
        if not isinstance(diagnostic, str):
            diagnostic = item.get("aggregated_output")
        if isinstance(diagnostic, str) and diagnostic:
            data["diagnostic"] = redact(diagnostic)[:1000]
    return AgentEvent(kind=AgentEventKind.TOOL_USE, data=data)


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


class CodexAdapter(CliAdapterBase):
    name = "codex"
    cli_name = "codex"

    async def _require_activated_runtime(self):
        """Evidence-bound gate; no user/config/request/environment override.

        Refuses unless `<data_dir>/codex-activation.json` (written only by
        `orchestrator codex-attest` after every native containment check
        PASSed) matches the currently resolved binary (version + executable
        hash, computed here and cached by stat fingerprint) and the running
        isolation source. Never falls back to the legacy process runner.

        Returns `(exe, identity)`: the resolved executable that was checked and
        the stat fingerprint its hash was taken under. `execute` launches exactly
        that executable and re-verifies both immediately before process creation.
        """
        try:
            exe = self.resolve()
        except ExecutableResolutionError:
            raise UnsupportedOperation(codex_activation.refusal_message(
                "unavailable: Codex executable not resolved")) from None
        reason, identity = await codex_activation.check_activation(
            self.data_dir, exe, self.child_env(), self._activation_cache, version_probe=self._probe_version)
        if reason or identity is None:
            raise UnsupportedOperation(codex_activation.refusal_message(reason or "unavailable"))
        return exe, identity

    async def _probe_version(self, exe, env: dict[str, str]) -> str | None:
        return await codex_activation.probe_version(exe, env)

    async def _check_activation(self):
        """Run the gate; a test harness may replace it with a synchronous stub (which returns None)."""
        outcome = self._require_activated_runtime()
        if inspect.isawaitable(outcome):
            outcome = await outcome
        return outcome

    def check_request(self, request: ExecutionRequest) -> None:
        super().check_request(request)
        if request.resume_session_id and request.extra_dirs:
            raise UnsupportedOperation("Codex resume cannot apply extra_dirs with this CLI interface")

    def __init__(self, *, persist_sessions: bool = True, task_capabilities: list[str] | None = None,
                 isolate_user_tools: bool = True, **kw: Any):
        super().__init__(**kw)
        self.persist_sessions = persist_sessions  # False adds --ephemeral (disables resume)
        self.task_capabilities = task_capabilities or []
        # True: a run does not load the user's Codex plugins / MCP servers (they inflated a tiny task to ~90k input tokens).
        self.isolate_user_tools = isolate_user_tools
        self.user_mcp_servers: list[str] = []  # names found by detect(); each is disabled per invocation
        self._run_mcp_servers: dict[str, tuple[str, ...]] = {}
        self._owned_contexts: dict[str, CodexRunContext] = {}
        self._activation_cache = codex_activation.BinaryIdentityCache()
        self._gated: dict[str, tuple] = {}  # run id -> (gate-checked executable, its binary identity)

    def _new_owned_context(self, request: ExecutionRequest):
        return CodexRunContext(self.data_dir, request.cwd, tuple(request.extra_dirs), self.child_env())

    async def _prepare_owned_context(self, context) -> None:
        environment = dict(self._environ if self._environ is not None else os.environ)
        try:
            await _owned_native(context, _prepare_owned, context, environment)
        finally:
            environment.clear()

    async def before_spawn(self, request: ExecutionRequest, spec: CommandSpec) -> None:
        context = self._owned_contexts.get(request.run_id)
        if context is None or spec.environment is None:
            raise CodexRuntimeUnavailable("owned invocation is missing")
        gated = self._gated.get(request.run_id)
        if gated is not None:
            # The launched binary must be the gate-checked one: same resolution, files unchanged since hashing.
            exe, identity = gated
            if self.resolve() != exe or not await asyncio.to_thread(codex_activation.unchanged_since, exe, identity):
                raise CodexRuntimeUnavailable("Codex executable changed after the activation check")
        await _owned_native(context, context.validate_before_launch)

    async def cancel(self, run_id: str) -> bool:
        context = self._owned_contexts.get(run_id)
        if context is not None:
            context.cancel_pending_start()
            self._cancelled.add(run_id)
        if run_id in self._procs or run_id in self._spawns:
            return await super().cancel(run_id)
        return context is not None

    def _isolation_args(self, run_id: str | None = None) -> list[str]:
        if not self.isolate_user_tools:
            return []
        args: list[str] = []
        for feature in ISOLATE_DISABLED_FEATURES:
            args += ["--disable", feature]
        names = self._run_mcp_servers.get(run_id, tuple(self.user_mcp_servers))
        if names:
            # Codex splits dotted override paths without TOML key quoting. An
            # inline table preserves dots/spaces/quotes in real server names.
            table = ",".join(json.dumps(name, ensure_ascii=False) + "={enabled=false}" for name in names)
            args += ["-c", "mcp_servers={" + table + "}"]
        return args

    async def execute(self, request: ExecutionRequest) -> AsyncIterator[AgentEvent]:
        environ = self._environ if self._environ is not None else os.environ
        check_recursion_guard(dict(environ))
        try:
            gated = await self._check_activation()
        except UnsupportedOperation as exc:
            message = str(exc)
            yield AgentEvent(kind=AgentEventKind.ERROR,
                             data={"category": ErrorCategory.INVALID_REQUEST.value, "message": message})
            yield self._result_event(ExecutionResult(success=False, error_category=ErrorCategory.INVALID_REQUEST,
                error_message=message, usage={"_isolation_error": True}))
            return
        context = stream = None
        uncertain = False
        result_event = None
        try:
            self.check_request(request)
            if not self.isolate_user_tools:
                raise UnsupportedOperation("owned Codex execution requires tool isolation")
            if request.run_id in self._owned_contexts:
                raise UnsupportedOperation("Codex run already owns an invocation")
            # The gate-checked executable is the one used below and launched (before_spawn re-verifies it).
            exe = gated[0] if gated else self.resolve()
            context = self._new_owned_context(request)
            self._owned_contexts[request.run_id] = context
            if gated:
                self._gated[request.run_id] = gated
            await self._prepare_owned_context(context)
            await _owned_native(context, context.validate_before_launch)
            try:
                names = await self._mcp_inventory(exe.argv_prefix, context.environment, cwd=request.cwd)
            except BaseException:
                # Interrupted helper cleanup is not assumed to have drained.
                uncertain = True
                raise
            if names != ():
                raise CodexRuntimeUnavailable("owned Codex MCP inventory must be empty")
            stream = super().execute(request)
            async for event in stream:
                if event.kind == AgentEventKind.RESULT:
                    result_event = event  # success is observable only AFTER drain and auth cleanup
                else:
                    yield event
        except CodexRuntimeCancelled:
            result_event = self._result_event(ExecutionResult(success=False,
                error_category=ErrorCategory.CANCELLED, error_message="owned Codex invocation cancelled"))
        except (CodexRuntimeUnavailable, CodexAuthUnavailable, UnsupportedOperation, ExecutableResolutionError):
            message = "Codex owned execution unavailable: preparation or isolation validation refused"
            yield AgentEvent(kind=AgentEventKind.ERROR,
                             data={"category": ErrorCategory.INVALID_REQUEST.value, "message": message})
            yield self._result_event(ExecutionResult(success=False, error_category=ErrorCategory.INVALID_REQUEST,
                error_message=message, usage={"_isolation_error": True}))
        except worker_job.ContainmentError:
            uncertain = True
            raise
        except GeneratorExit:
            raise  # inner aclose below still proves drain for an abandoned event stream
        except BaseException:
            uncertain |= stream is not None
            raise
        finally:
            try:
                if stream is not None:
                    try:
                        await stream.aclose()
                    except BaseException:
                        uncertain = True
                        raise
            finally:
                if context is not None:
                    try:
                        await _owned_native(context, context.close, uncertain, failure_wins=True)
                    except CodexRuntimeUnavailable:
                        raise worker_job.ContainmentError("owned Codex runtime cleanup uncertain") from None
                    except asyncio.CancelledError:
                        if uncertain:
                            raise worker_job.ContainmentError("owned Codex lifetime uncertain; runtime retained") from None
                        raise
                    finally:
                        if request.run_id in self._cancelled and result_event is not None:
                            result_event = self._result_event(ExecutionResult(success=False,
                                error_category=ErrorCategory.CANCELLED,
                                error_message="owned Codex invocation cancelled during cleanup"))
                        self._owned_contexts.pop(request.run_id, None)
                        self._gated.pop(request.run_id, None)
                        self._cancelled.discard(request.run_id)
                    if uncertain:
                        raise worker_job.ContainmentError("owned Codex lifetime uncertain; runtime retained") from None
        if result_event is not None:
            yield result_event

    def build_command(self, request: ExecutionRequest) -> CommandSpec:
        # Session id (finding #13): validated only. `exec resume` takes it as a positional; --help shows no
        # attached form, so a leading '-' is what validation forbids.
        self.check_request(request)
        context = self._owned_contexts.get(request.run_id)
        if context is not None:
            return self._build_owned_command(request, context)
        cleanup: list[Path] = []
        base = ["--skip-git-repo-check", "--json", *SECURITY_ARGS, *self._isolation_args(request.run_id)]
        if request.resume_session_id:
            # `codex exec resume` has neither -s nor -C: sandbox goes through a config override,
            # the working directory is the process cwd. The prompt is read from stdin via "-".
            args = ["exec", "resume", *base, "-c", f'sandbox_mode="{SANDBOX[request.permission]}"']
        else:
            args = ["exec", *base, "-s", SANDBOX[request.permission], "-C", request.cwd]
            if not self.persist_sessions:
                # Verified: an --ephemeral thread cannot be resumed ("no rollout found for thread id").
                args.append("--ephemeral")
            for d in request.extra_dirs:
                args += ["--add-dir", d]
        if request.model:
            args += ["-m", request.model]
        if request.json_schema is not None:
            fd, name = tempfile.mkstemp(prefix="orch-schema-", suffix=".json", dir=self.schema_dir())
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(request.json_schema, fh)
            cleanup.append(Path(name))
            args += ["--output-schema", name]
        if request.resume_session_id:
            args += [request.resume_session_id, "-"]
        return CommandSpec(args=args, stdin_text=request.prompt, cleanup=cleanup)

    def _build_owned_command(self, request: ExecutionRequest, context) -> CommandSpec:
        environment = context.environment
        permission = {"read_only": "orch-read", "workspace_write": "orch-write"}[request.permission]
        args = ["exec"]
        if request.resume_session_id:
            args.append("resume")
        args += ["--skip-git-repo-check", "--json", *SECURITY_ARGS,
                 "--disable", "plugins", "--disable", "apps", "--disable", "multi_agent",
                 "-c", 'windows.sandbox="mxc"', "-c", f'default_permissions="{permission}"']
        if not request.resume_session_id:
            args += ["-C", request.cwd]
            if not self.persist_sessions:
                args.append("--ephemeral")
            for directory in request.extra_dirs:
                args += ["--add-dir", directory]
        if request.model:
            args += ["-m", request.model]
        cleanup = []
        if request.json_schema is not None:
            fd, name = tempfile.mkstemp(prefix="schema-", suffix=".json", dir=context.home)
            cleanup.append(Path(name))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as target:
                    json.dump(request.json_schema, target)
            except BaseException:
                Path(name).unlink(missing_ok=True)
                raise
            args += ["--output-schema", name]
        if request.resume_session_id:
            args += [request.resume_session_id, "-"]
        return CommandSpec(args=args, stdin_text=request.prompt, cleanup=cleanup, environment=environment)

    def schema_dir(self) -> str | None:
        """Where `--output-schema` files are written: `<data dir>/tmp` when this adapter knows the server's data
        dir (production always passes it), else the system temp dir."""
        if os.environ.get(SCHEMA_IN_TEMP_ENV, "").strip() == "1" or self._data_dir is None:
            return None
        d = self._data_dir / "tmp"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def parse_line(self, line: str, state: ParseState) -> list[AgentEvent]:
        return parse_codex_line(line, state)

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
        env = self.child_env()
        code, out, err = await run_capture([*exe.argv_prefix, "--version"], env=env)
        m = re.search(r"\d+\.\d+\.\d+", out)
        status = ProviderStatus(provider=self.name, installed=code == 0, version=m.group(0) if m else None,
                                executable=exe.path, capabilities=caps,
                                error=None if code == 0 else (err.strip()[:300] or "version check failed"))
        if code != 0:
            return status
        code, out, err = await run_capture([*exe.argv_prefix, "login", "status"], env=env)
        text = (out + "\n" + err).strip()
        if code == 0 and re.search(r"logged in", text, re.IGNORECASE):
            status.authenticated = True
            status.auth_detail = text.splitlines()[0][:120] if text else None
        elif re.search(r"not logged in", text, re.IGNORECASE):
            status.authenticated = False
            status.auth_detail = "not logged in"
        try:
            await self._check_activation()
        except UnsupportedOperation as exc:
            status.error = str(exc)
            status.capabilities.sandbox_modes = []
            return status
        # Effective tool inventory belongs to a prepared, pinned invocation.
        # Discovery must not inspect/adopt the user's mutable MCP configuration.
        self.user_mcp_servers = []
        return status

    async def _mcp_inventory(self, prefix, env: dict[str, str], *, cwd: str | None = None) -> tuple[str, ...] | None:
        """Read effective configuration without starting a model or MCP server.

        Never log raw JSON: transports may contain credentials. A helper
        ContainmentError propagates to the executor's quarantine unchanged.
        """
        flags = [a for f in ("hooks", *ISOLATE_DISABLED_FEATURES) for a in ("--disable", f)]
        code, out, _ = await run_capture([*prefix, *flags, "mcp", "list", "--json"],
                                        cwd=cwd, env=env, timeout=30)
        if code != 0:
            return None
        try:
            servers = json.loads(out)
            if not isinstance(servers, list) or len(servers) > 512:
                return None
            if any(not isinstance(s, dict) or not isinstance(s.get("name"), str) or not s["name"] for s in servers):
                return None
            names = tuple(sorted({s["name"] for s in servers}))
            table = ",".join(json.dumps(name, ensure_ascii=False) + "={enabled=false}" for name in names)
            if len(table.encode("utf-16-le")) > 16_000:
                return None  # bounded command line; never silently truncate
            return names
        except (ValueError, KeyError, TypeError, UnicodeError):
            return None

    async def _refresh_user_mcp(self, prefix, env: dict[str, str]) -> None:
        names = await self._mcp_inventory(prefix, env)
        self.user_mcp_servers = list(names) if names is not None else []

    def _seed(self) -> dict[str, dict[str, Any]]:
        try:
            data = yaml.safe_load((repo_config_dir() / "models.yaml").read_text(encoding="utf-8"))
            return {m["id"]: m for m in data["providers"]["codex"]["models"]}
        except Exception:  # noqa: BLE001
            return {}

    async def discover_models(self) -> list[ModelInfo]:
        seed = self._seed()
        caps = list(self.task_capabilities)
        try:
            exe = self.resolve()
            code, out, _ = await run_capture([*exe.argv_prefix, "debug", "models"], env=self.child_env(), timeout=45)
            if code == 0:
                catalog = json.loads(out)
                models: list[ModelInfo] = []
                for m in catalog.get("models", []):
                    if not isinstance(m, dict) or not m.get("slug") or m.get("visibility") not in (None, "list"):
                        continue
                    tier = seed.get(m["slug"], {}).get("tier", "standard")
                    models.append(ModelInfo(provider=self.name, model_id=m["slug"], display_name=m.get("display_name"),
                                            source="cli", tier=tier, capabilities=caps))
                if models:
                    return models
        except Exception:  # noqa: BLE001 - fall through to config
            pass
        try:
            cfg = tomllib.loads((_codex_home() / "config.toml").read_text(encoding="utf-8"))
            if isinstance(cfg.get("model"), str):
                mid = cfg["model"]
                return [ModelInfo(provider=self.name, model_id=mid, display_name=mid, source="config",
                                  tier=seed.get(mid, {}).get("tier", "standard"), capabilities=caps)]
        except Exception:  # noqa: BLE001
            pass
        return []
