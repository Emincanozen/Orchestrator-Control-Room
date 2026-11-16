"""Regressions found by real CLI checks on 2026-11-11 (agy 1.5.6, codex 0.167.1)."""
import tempfile
from pathlib import Path

from orchestrator.providers.antigravity import parse_agy_line
from orchestrator.providers.base import AgentEventKind, ErrorCategory, ExecutionRequest
from orchestrator.providers.antigravity import AntigravityAdapter
from orchestrator.providers.codex import CodexAdapter
from orchestrator.providers.process import ParseState

FIX = Path(__file__).parent.parent / "fixtures" / "providers"


def _parse(name):
    state, events = ParseState(), []
    for line in (FIX / name).read_text(encoding="utf-8").splitlines():
        events += parse_agy_line(line, state)
    return state, events


def test_agy_denied_actions_is_not_success():
    state, _ = _parse("agy_denied.jsonl")
    assert state.success is False
    msg = " ".join(state.error_messages)
    assert "RunCommand" in msg
    assert AntigravityAdapter().classify_error(0, "", msg) == ErrorCategory.PERMISSION_DENIED


def test_agy_tool_event_carries_real_tool_name_once():
    _, events = _parse("agy_denied.jsonl")
    tools = [e for e in events if e.kind == AgentEventKind.TOOL_USE]
    assert len(tools) == 1
    assert tools[0].data["tool"] == "run_command"
    assert "Get-ChildItem" in tools[0].data["summary"]


def _req(**kw):
    # cwd must not be inside the orchestrator checkout (re-review N3): use the system temp dir.
    return ExecutionRequest(run_id="r", prompt="p", cwd=tempfile.gettempdir(), permission="read_only", **kw)


def test_codex_sessions_persist_by_default_so_resume_works():
    assert "--ephemeral" not in CodexAdapter().build_command(_req()).args
    assert "--ephemeral" in CodexAdapter(persist_sessions=False).build_command(_req()).args


def test_agy_file_tool_path_is_extracted():
    line = ('{"event":"step_update","step_update":{"conversation_id":"c","step_index":3,"state":"ACTIVE","step_type":"tool",'
            '"tool_name":"write_file","tool_info":{"name":"write_file","parameters":{"Path":"C:/repo/hello.txt"}}}}')
    events = parse_agy_line(line, ParseState())
    tools = [e for e in events if e.kind == AgentEventKind.TOOL_USE]
    assert tools and tools[0].data["tool"] == "write_file" and tools[0].data["files"] == ["C:/repo/hello.txt"]
