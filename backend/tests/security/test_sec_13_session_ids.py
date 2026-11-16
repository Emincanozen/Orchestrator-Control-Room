"""Finding #13 (LOW): resume session ids are validated centrally and cannot be parsed as CLI options."""

from __future__ import annotations

import pytest

from orchestrator.providers.antigravity import AntigravityAdapter
from orchestrator.providers.base import AgentEventKind as K
from orchestrator.providers.base import ErrorCategory as C
from orchestrator.providers.base import ExecutionRequest
from orchestrator.providers.claude import ClaudeAdapter
from orchestrator.providers.codex import CodexAdapter
from orchestrator.providers.process import InvalidRequestError, ResolvedExecutable, validate_session_id

ADAPTERS = [ClaudeAdapter, CodexAdapter, AntigravityAdapter]

BAD = ["-x", "--dangerously-skip-permissions", "--settings={}", "-", "", "a b", "a\nb", "a\x00b", "a;b", "a&b",
       "a|b", "../x", "a/b", "a\\b", "$(x)", "`x`", "a\"b", "x" * 129, "é"]
GOOD = ["sess-9", "01a1cb9d-0000-4000-8000-000000000001", "thread_name.v2", "a:b", "X" * 128, "0"]


@pytest.mark.parametrize("sid", BAD)
def test_bad_ids_rejected(sid):
    with pytest.raises(InvalidRequestError):
        validate_session_id(sid)


@pytest.mark.parametrize("sid", GOOD)
def test_good_ids_accepted(sid):
    assert validate_session_id(sid) == sid


def _req(tmp_path, sid):
    return ExecutionRequest(run_id="r", prompt="p", cwd=str(tmp_path / "wt"), permission="workspace_write", resume_session_id=sid)


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize("sid", ["--dangerously-skip-permissions", "-p", "x" * 200])
def test_builders_refuse_bad_ids(tmp_path, adapter, sid):
    with pytest.raises(InvalidRequestError):
        adapter(data_dir=tmp_path / "data").build_command(_req(tmp_path, sid))


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_execute_turns_a_bad_id_into_invalid_request_without_spawning(tmp_path, adapter):
    calls = []

    async def spawner(argv, **kw):  # pragma: no cover - must not be reached
        calls.append(argv)
        raise OSError("no")

    ad = adapter(spawner=spawner, env_allowlist=["PATH"], environ={}, data_dir=tmp_path / "data",
                 resolver=lambda n: ResolvedExecutable(("x",), "x", "test"))
    evs = [e async for e in ad.execute(_req(tmp_path, "--evil"))]
    assert calls == [] and evs[-1].kind == K.RESULT
    assert evs[-1].data["error_category"] == C.INVALID_REQUEST.value


def test_attached_forms(tmp_path):
    a = ClaudeAdapter(data_dir=tmp_path / "data").build_command(_req(tmp_path, "sess-9")).args
    assert "--resume=sess-9" in a and "--resume" not in a
    g = AntigravityAdapter(data_dir=tmp_path / "data").build_command(_req(tmp_path, "sess-9")).args
    assert "--conversation=sess-9" in g and "--conversation" not in g
    c = CodexAdapter(data_dir=tmp_path / "data").build_command(_req(tmp_path, "sess-9")).args
    assert c[:2] == ["exec", "resume"] and c[-2:] == ["sess-9", "-"]  # positional; validation forbids a leading '-'
