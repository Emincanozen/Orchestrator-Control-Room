"""Real CLI smoke tests: one tiny read_only call per provider. Opt-in: `pytest -m smoke tests/smoke`.

Spends a few tokens of each subscription. Skipped when the CLI is not installed.
"""

from __future__ import annotations

import pytest

from orchestrator.providers.antigravity import AntigravityAdapter
from orchestrator.providers.base import AgentEventKind as K
from orchestrator.providers.base import ExecutionRequest
from orchestrator.providers.claude import ClaudeAdapter
from orchestrator.providers.codex import CodexAdapter
from orchestrator.providers.process import ExecutableResolutionError

pytestmark = pytest.mark.smoke

CASES = {
    "claude": (lambda: ClaudeAdapter(persist_sessions=False), "haiku"),
    "codex": (lambda: CodexAdapter(), None),  # no cheaper model is known; the configured default is used
    "antigravity": (lambda: AntigravityAdapter(), "gemini-3.8-flash-low"),
}


@pytest.mark.parametrize("provider", list(CASES))
async def test_real_provider_roundtrip(provider, tmp_path):
    factory, model = CASES[provider]
    adapter = factory()
    try:
        adapter.resolve()
    except ExecutableResolutionError as exc:
        pytest.skip(str(exc))
    req = ExecutionRequest(run_id=f"smoke-{provider}", prompt="Reply with exactly: X_OK", cwd=str(tmp_path),
                           model=model, permission="read_only", timeout_seconds=150)
    events = [e async for e in adapter.execute(req)]
    kinds = [e.kind for e in events]
    result = events[-1].data
    if result.get("error_category") in ("quota_exhausted", "rate_limit"):
        pytest.fail(f"{provider} reports a usage limit: {result.get('error_message')}")
    assert K.SESSION in kinds, [e.model_dump() for e in events][:5]
    assert kinds[-1] == K.RESULT
    assert result["success"] is True, result
    assert "X_OK" in result["final_text"]
    assert result["session_id"]
