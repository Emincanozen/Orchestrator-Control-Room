"""One contract suite, run against all three adapters with recorded streams and a fake process layer."""

from __future__ import annotations

import json

import pytest

from orchestrator.providers.base import AgentEventKind as K
from orchestrator.providers.base import ErrorCategory as C
from orchestrator.providers.base import RecursionGuardError

from .conftest import BASE_ENV, SECRET_NAME, FakeSpawner, collect, fixture_lines, make_adapter, request

SUCCESS = {"claude": "claude_success.jsonl", "codex": "codex_success.jsonl", "antigravity": "agy_success.jsonl"}
FAILURE = {"claude": "claude_quota.jsonl", "codex": "codex_quota.jsonl", "antigravity": "agy_error.jsonl"}
EXPECT_TEXT = {"claude": "CLAUDE_OK", "codex": "CODEX_OK", "antigravity": "AGY_OK"}
EXPECT_SESSION = {"claude": "sess-claude-1", "codex": "01a1cb9d-0000-4000-8000-000000000001",
                  "antigravity": "3f3f0000-aaaa-bbbb-cccc-000000000001"}
EXPECT_TOKENS = {"claude": (2, 11), "codex": (21263, 7), "antigravity": (9164, 4)}


async def run_success(provider, **kw):
    sp = FakeSpawner(fixture_lines(SUCCESS[provider]))
    ad = make_adapter(provider, sp)
    return ad, sp, await collect(ad, request(**kw))


async def test_event_mapping_and_result(provider):
    _, _, evs = await run_success(provider)
    kinds = [e.kind for e in evs]
    assert kinds[0] == K.SESSION
    assert evs[0].data["session_id"] == EXPECT_SESSION[provider]
    assert K.TEXT in kinds and K.USAGE in kinds
    assert kinds[-1] == K.RESULT and kinds.count(K.RESULT) == 1
    res = evs[-1].data
    assert res["success"] is True and res["final_text"].strip() == EXPECT_TEXT[provider]
    assert res["session_id"] == EXPECT_SESSION[provider]
    assert res["exit_code"] == 0 and res["error_category"] is None
    usage = next(e for e in evs if e.kind == K.USAGE).data
    assert (usage["input_tokens"], usage["output_tokens"]) == EXPECT_TOKENS[provider]
    assert res["usage"]["input_tokens"] == EXPECT_TOKENS[provider][0]


async def test_unknown_and_malformed_lines_become_raw_not_errors(provider):
    _, _, evs = await run_success(provider)
    raws = [e for e in evs if e.kind == K.RAW]
    assert raws, "fixture contains unknown/malformed lines that must surface as RAW"
    assert all("line" in e.data for e in raws)


async def test_tool_use_with_files():
    _, _, evs = await run_success("claude")
    tools = [e.data for e in evs if e.kind == K.TOOL_USE]
    assert tools[0]["tool"] == "Read" and tools[0]["files"] == ["src/app.py"]
    assert tools[1]["tool"] == "Bash" and "pytest" in tools[1]["summary"] and "files" not in tools[1]
    _, _, evs = await run_success("codex")
    tools = [e.data for e in evs if e.kind == K.TOOL_USE]
    assert any(t.get("files") == ["src/a.py"] for t in tools)
    shell = [t for t in tools if t["tool"] == "shell"]
    assert [t["stage"] for t in shell] == ["started", "completed"]
    assert len({t["tool_call_id"] for t in shell}) == 1  # One call, two lifecycle observations.
    _, _, evs = await run_success("antigravity")
    tools = [e.data for e in evs if e.kind == K.TOOL_USE]
    assert tools and tools[0]["files"] == ["C:\\tmp\\a.txt"]


async def test_claude_rate_limit_and_cost_estimate():
    _, _, evs = await run_success("claude")
    rl = next(e for e in evs if e.kind == K.RATE_LIMIT).data
    assert rl["status"] == "allowed" and rl["utilization"] == 0.06 and rl["window"] == "five_hour"
    assert rl["resets_at"] == 1794423600
    usage = next(e for e in evs if e.kind == K.USAGE).data
    assert usage["cost_usd"] == pytest.approx(0.00085899) and usage["cost_basis"] == "list_price_equivalent"
    assert usage["cached_input_tokens"] == 649


async def test_result_last_and_failed_on_error_stream(provider):
    sp = FakeSpawner(fixture_lines(FAILURE[provider]), returncode=1)
    evs = await collect(make_adapter(provider, sp), request())
    assert evs[-1].kind == K.RESULT and sum(e.kind == K.RESULT for e in evs) == 1
    assert evs[-2].kind == K.ERROR
    res = evs[-1].data
    assert res["success"] is False
    assert res["error_category"] == C.QUOTA_EXHAUSTED.value
    assert res["session_id"]


@pytest.mark.parametrize("lines,rc,stderr,expected", [
    ([], 0, "", C.CRASHED),  # exits 0 but never reports a result
    (["garbage"], 1, "", C.UNKNOWN),
    ([], 3221225477, "", C.CRASHED),  # access-violation style exit code
    ([], 1, "getaddrinfo ENOTFOUND api.example.com", C.NETWORK),
])
async def test_result_always_last_without_provider_result(provider, lines, rc, stderr, expected):
    sp = FakeSpawner(lines, returncode=rc, stderr=stderr)
    evs = await collect(make_adapter(provider, sp), request())
    assert evs[-1].kind == K.RESULT and evs[-1].data["success"] is False
    assert evs[-1].data["error_category"] == expected.value


async def test_spawn_failure_still_ends_with_result(provider):
    async def boom(*a, **k):
        raise OSError("no such file")

    evs = await collect(make_adapter(provider, boom), request())
    assert [e.kind for e in evs] == [K.ERROR, K.RESULT]
    assert evs[-1].data["error_category"] == C.CRASHED.value


async def test_timeout_flag_maps_to_timeout(provider):
    class TimedOut(FakeSpawner):
        async def __call__(self, argv, **kw):
            p = await super().__call__(argv, **kw)
            p.timed_out = True
            return p

    evs = await collect(make_adapter(provider, TimedOut([], returncode=1)), request(timeout_seconds=5))
    assert evs[-1].data["error_category"] == C.TIMEOUT.value


# --------------------------------------------------------------------------- classification

CLASSIFY = {
    "claude": [
        ("Claude AI usage limit reached|1794423600", C.QUOTA_EXHAUSTED),
        ("5-hour limit reached - resets 3pm", C.QUOTA_EXHAUSTED),
        ("API Error: 429 rate_limit_error: slow down", C.RATE_LIMIT),
        ("Overloaded", C.RATE_LIMIT),
        ("Invalid API key - Please run /login", C.AUTH),
        ("Not logged in", C.AUTH),
        ("OAuth token has expired", C.AUTH),
        ("connect ECONNREFUSED 1.2.3.4:443", C.NETWORK),
        ("There's an issue with the selected model (foo)", C.INVALID_REQUEST),
        ("error: unknown option '--bogus'", C.INVALID_REQUEST),
        ("No conversation found with session ID", C.INVALID_REQUEST),
        ("tool use was denied: permission denied", C.PERMISSION_DENIED),
        ("something odd happened", C.UNKNOWN),
    ],
    "codex": [
        ("You've hit your usage limit. Upgrade to Pro", C.QUOTA_EXHAUSTED),
        ("exceeded retry limit, last status: 429 Too Many Requests", C.RATE_LIMIT),
        ("Not logged in. Run codex login", C.AUTH),
        ("401 Unauthorized", C.AUTH),
        ("stream disconnected before completion", C.NETWORK),
        ("unexpected argument '--nope' found", C.INVALID_REQUEST),
        ("model_not_found: gpt-x does not exist", C.INVALID_REQUEST),
        ("blocked by sandbox", C.PERMISSION_DENIED),
        ("weird", C.UNKNOWN),
    ],
    "antigravity": [
        ("rpc error: code = ResourceExhausted desc = quota exceeded", C.QUOTA_EXHAUSTED),
        ("429 too many requests", C.RATE_LIMIT),
        ("UNAUTHENTICATED: please sign in", C.AUTH),
        ("dial tcp: getaddrinfo failed", C.NETWORK),
        ("flag provided but not defined: -zzz", C.INVALID_REQUEST),
        ("Error: empty prompt. Usage: agy --print", C.INVALID_REQUEST),
        ("weird", C.UNKNOWN),
    ],
}


@pytest.mark.parametrize("prov,text,expected", [(p, t, c) for p, rows in CLASSIFY.items() for t, c in rows])
def test_error_classification_table(prov, text, expected):
    ad = make_adapter(prov, FakeSpawner([]))
    assert ad.classify_error(1, "", text) == expected
    assert ad.classify_error(1, text, "") == expected


def test_not_everything_is_quota(provider):
    ad = make_adapter(provider, FakeSpawner([]))
    assert ad.classify_error(1, "", "totally unrelated failure") not in (C.QUOTA_EXHAUSTED, C.RATE_LIMIT)
    assert ad.classify_error(None, "", "") == C.UNKNOWN


# --------------------------------------------------------------------------- command construction


def args_for(provider, **kw):
    ad = make_adapter(provider, FakeSpawner([]))
    return ad.build_command(request(**kw)).args


def flag_value(args, flag):
    return args[args.index(flag) + 1]


def test_permission_mapping_read_only():
    a = args_for("claude", permission="read_only")
    assert flag_value(a, "--permission-mode") == "plan" and "--strict-mcp-config" in a
    assert "Bash" not in flag_value(a, "--tools") and "Write" not in flag_value(a, "--tools")
    assert flag_value(args_for("codex", permission="read_only"), "-s") == "read-only"
    assert flag_value(args_for("antigravity", permission="read_only"), "--mode") == "plan"


def test_permission_mapping_workspace_write():
    a = args_for("claude", permission="workspace_write")
    assert flag_value(a, "--permission-mode") == "acceptEdits" and flag_value(a, "--permission-prompts") == "none"
    assert "--strict-mcp-config" in a
    tools = flag_value(a, "--tools").split(",")
    # finding #3: file tools only by default (Bash needs the YAML-only claude_shell policy)
    assert {"Write", "Edit", "Read"} <= set(tools) and "Bash" not in tools
    assert flag_value(a, "--allowedTools") == flag_value(a, "--tools") and "--restricted" in a
    assert flag_value(args_for("codex", permission="workspace_write"), "-s") == "workspace-write"
    assert flag_value(args_for("antigravity", permission="workspace_write"), "--mode") == "accept-edits"


def test_dangerous_flags_never_used(provider):
    joined = " ".join(args_for(provider, permission="workspace_write"))
    for bad in ("--dangerously", "bypass", "danger-full-access", "bypassPermissions"):
        assert bad not in joined


def test_model_extra_dirs_and_resume(provider):
    extra_dirs = [] if provider == "codex" else ["D:\\extra"]
    a = args_for(provider, model="m-1", extra_dirs=extra_dirs, resume_session_id="sess-9")
    assert "m-1" in a
    # finding #13: attached `--flag=<id>` form where the CLI parser accepts it
    if provider == "claude":
        assert "--resume=sess-9" in a and flag_value(a, "--add-dir") == "D:\\extra"
    elif provider == "antigravity":
        assert "--conversation=sess-9" in a and flag_value(a, "--add-dir") == "D:\\extra"
    else:
        assert a[:2] == ["exec", "resume"] and a[-2:] == ["sess-9", "-"]
        fresh = args_for(provider, model="m-1", extra_dirs=["D:\\extra"])
        assert flag_value(fresh, "--add-dir") == "D:\\extra"


def test_json_schema_support(tmp_path):
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    assert json.loads(flag_value(args_for("claude", json_schema=schema), "--json-schema")) == schema
    assert json.loads(flag_value(args_for("antigravity", json_schema=schema), "--json-schema")) == schema
    adapter = make_adapter("codex", FakeSpawner([]))
    adapter._data_dir = tmp_path / "isolated-schema-data"
    spec = adapter.build_command(request(json_schema=schema))
    try:
        path = flag_value(spec.args, "--output-schema")
        with open(path, encoding="utf-8") as fh:
            assert json.load(fh) == schema
    finally:
        for p in spec.cleanup:
            p.unlink(missing_ok=True)


async def test_oversized_schema_is_invalid_request_result():
    big = {"type": "object", "description": "x" * 30000}
    evs = await collect(make_adapter("claude", FakeSpawner([])), request(json_schema=big))
    assert evs[-1].kind == K.RESULT and evs[-1].data["error_category"] == C.INVALID_REQUEST.value


async def test_prompt_never_in_argv(provider):
    prompt = 'UNIQUE-PROMPT-MARKER "quoted" & calc.exe | whoami'
    sp = FakeSpawner(fixture_lines(SUCCESS[provider]))
    await collect(make_adapter(provider, sp), request(prompt=prompt))
    call = sp.calls[0]
    assert not any("UNIQUE-PROMPT-MARKER" in a for a in call["argv"])
    assert "UNIQUE-PROMPT-MARKER" in call["stdin"]
    assert call["cwd"] == "C:\\work"


def test_agy_stdin_message_shape():
    spec = make_adapter("antigravity", FakeSpawner([])).build_command(request(prompt="hello\nworld"))
    assert json.loads(spec.stdin_text) == {"event": "user", "message": {"content": "hello\nworld"}}
    assert spec.stdin_text.endswith("\n") and spec.args[0] == "-p="


# --------------------------------------------------------------------------- guard + env


async def test_recursion_guard_refuses_nested_start(provider):
    sp = FakeSpawner(fixture_lines(SUCCESS[provider]))
    ad = make_adapter(provider, sp, environ={**BASE_ENV, "AI_ORCHESTRATOR_DEPTH": "1"})
    with pytest.raises(RecursionGuardError):
        await collect(ad, request())
    assert sp.calls == []


async def test_child_env_is_allowlisted_and_depth_set(provider):
    sp = FakeSpawner(fixture_lines(SUCCESS[provider]))
    await collect(make_adapter(provider, sp), request())
    env = sp.calls[0]["env"]
    assert SECRET_NAME not in env and "OPENAI_API_KEY" not in env
    assert env["PATH"] == "C:\\bin" and env["AI_ORCHESTRATOR_DEPTH"] == "1"
    assert set(env) <= {"PATH", "SYSTEMROOT", "USERPROFILE", "AI_ORCHESTRATOR_DEPTH"}


async def test_pid_for_and_cancel_unknown_run(provider):
    ad = make_adapter(provider, FakeSpawner([]))
    assert ad.pid_for("nope") is None
    assert await ad.cancel("nope") is False
