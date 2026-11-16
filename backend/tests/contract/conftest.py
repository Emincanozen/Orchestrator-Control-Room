"""Shared fakes for the provider contract suite (no real CLI is ever started here)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from orchestrator.providers.antigravity import AntigravityAdapter
from orchestrator.providers.base import ExecutionRequest
from orchestrator.providers.claude import ClaudeAdapter
from orchestrator.providers.codex import CodexAdapter
from orchestrator.providers.process import ResolvedExecutable

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "providers"

SECRET_NAME = "ORCH_TEST_SECRET_TOKEN"


class FakeProcess:
    """ProcessHandle that replays recorded stdout lines."""

    def __init__(self, stdout_lines: list[str], *, returncode: int = 0, stderr: str = "", pid: int = 4242):
        self._lines = stdout_lines
        self._stderr = stderr
        self.pid: int | None = pid
        self.returncode: int | None = None
        self._final_rc = returncode
        self.timed_out = False
        self.killed = False

    async def stream(self) -> AsyncIterator[tuple[str, str]]:
        for ln in self._lines:
            yield "stdout", ln
        for ln in self._stderr.splitlines():
            yield "stderr", ln

    @property
    def stdout_tail(self) -> str:
        return "\n".join(self._lines)

    @property
    def stderr_tail(self) -> str:
        return self._stderr

    async def wait(self) -> int:
        self.returncode = self._final_rc
        return self._final_rc

    async def terminate_tree(self) -> bool:
        self.killed = True
        return True


class FakeSpawner:
    def __init__(self, lines: list[str], **proc_kw):
        self.lines = lines
        self.proc_kw = proc_kw
        self.calls: list[dict] = []

    async def __call__(self, argv, *, cwd, env, stdin_text=None, timeout=None):
        self.calls.append({"argv": list(argv), "cwd": cwd, "env": dict(env), "stdin": stdin_text, "timeout": timeout})
        return FakeProcess(self.lines, **self.proc_kw)


def fixture_lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


def fake_resolver(name: str) -> ResolvedExecutable:
    return ResolvedExecutable((f"C:\\fake\\{name}.exe",), f"C:\\fake\\{name}.exe", "test")


ALLOW = ["PATH", "SYSTEMROOT", "USERPROFILE"]
BASE_ENV = {"PATH": "C:\\bin", "SYSTEMROOT": "C:\\Windows", "USERPROFILE": "C:\\Users\\x",
            SECRET_NAME: "super-secret", "OPENAI_API_KEY": "sk-leak"}

ADAPTERS = {"claude": ClaudeAdapter, "codex": CodexAdapter, "antigravity": AntigravityAdapter}


def make_adapter(provider: str, spawner, environ: dict | None = None, *, stub_mcp: bool = True):
    adapter = ADAPTERS[provider](spawner=spawner, env_allowlist=ALLOW,
                                environ=environ if environ is not None else dict(BASE_ENV), resolver=fake_resolver)
    if provider == "codex":
        # Explicit fake-transport harness only. This does not activate or attest
        # any production runtime; default-adapter gating has separate tests.
        adapter._require_activated_runtime = lambda: None
        class FakeOwnedContext:
            def __init__(self):
                self.environment = adapter.child_env()

            def validate_before_launch(self):
                pass

            def cancel_pending_start(self):
                pass

            def close(self, retain=False):
                pass

        adapter._new_owned_context = lambda request: FakeOwnedContext()

        async def prepare_fake(context):
            pass

        adapter._prepare_owned_context = prepare_fake
    if provider == "codex" and stub_mcp:
        # The fake CLI transport includes a successful empty metadata inventory;
        # contract tests must never accidentally invoke a real CLI for preflight.
        async def empty_inventory(*args, **kwargs):
            return ()
        adapter._mcp_inventory = empty_inventory
    return adapter


def request(**kw) -> ExecutionRequest:
    base = dict(run_id="run-1", prompt="Reply with exactly: X_OK", cwd="C:\\work", permission="read_only")
    base.update(kw)
    return ExecutionRequest(**base)


async def collect(adapter, req):
    return [e async for e in adapter.execute(req)]


@pytest.fixture(params=["claude", "codex", "antigravity"])
def provider(request):
    return request.param
