"""Finding #1 (HIGH): agents must not be able to reach the API token / DB.

Covers decision A (data location, worktrees outside the data dir), C (scratch cwd, central run-path guard,
child environment) and the YAML-only `claude_shell` switch (an agent holding the token cannot turn its own
shell on through the settings API).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orchestrator import config
from orchestrator.api.app import create_app
from orchestrator.config import Paths, PathsConfigError, effective_policies, get_paths
from orchestrator.providers.base import AgentEventKind as K
from orchestrator.providers.base import ErrorCategory as C
from orchestrator.providers.base import ExecutionRequest
from orchestrator.providers.mock import MockProvider
from orchestrator.providers.process import InvalidRequestError, build_child_env, check_run_paths

from orchestrator.providers.antigravity import AntigravityAdapter
from orchestrator.providers.claude import ClaudeAdapter
from orchestrator.providers.codex import CodexAdapter
from orchestrator.providers.process import ResolvedExecutable

BASE = "http://127.0.0.1:8765"
ADAPTERS = {"claude": ClaudeAdapter, "codex": CodexAdapter, "antigravity": AntigravityAdapter}


class FakeSpawner:
    """Records spawn calls; these tests must never get that far."""

    def __init__(self, _lines):
        self.calls: list = []

    async def __call__(self, argv, **kw):  # pragma: no cover - reaching this is the failure
        self.calls.append(argv)
        raise OSError("spawn must not happen in this test")


def fake_resolver(name: str) -> ResolvedExecutable:
    return ResolvedExecutable((f"C:\\fake\\{name}.exe",), f"C:\\fake\\{name}.exe", "test")


# --------------------------------------------------------------------------- A: layout


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def test_default_data_dir_is_outside_the_checkout(monkeypatch, tmp_path):
    monkeypatch.delenv(config.ENV_DATA_DIR, raising=False)
    monkeypatch.delenv(config.ENV_WORKTREES_DIR, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    p = get_paths()
    expected = (tmp_path / "lad" / "AI-Orchestrator") if os.name == "nt" else (tmp_path / "xdg" / "ai-orchestrator")
    assert p.data_dir == expected.resolve()
    assert p.worktrees_dir == expected.resolve().parent / f"{expected.name}-worktrees"
    assert not str(p.data_dir).lower().startswith(str(config.REPO_ROOT).lower())


def test_worktrees_are_a_sibling_not_a_child_of_the_data_dir(tmp_path):
    p = get_paths(tmp_path / "data")
    assert p.worktrees_dir == (tmp_path / "data-worktrees").resolve()
    assert not str(p.worktrees_dir).lower().startswith(str(p.data_dir).lower() + os.sep)
    for secret in (p.db_path, p.token_path, p.logs_dir, p.skills_dir):
        assert secret.parent == p.data_dir or secret.parent.parent == p.data_dir
    assert not _inside(p.leader_scratch_dir, p.data_dir)


def test_env_overrides_apply_only_without_an_explicit_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv(config.ENV_DATA_DIR, str(tmp_path / "d"))
    monkeypatch.setenv(config.ENV_WORKTREES_DIR, str(tmp_path / "w"))
    p = get_paths()
    assert (p.data_dir, p.worktrees_dir) == ((tmp_path / "d").resolve(), (tmp_path / "w").resolve())
    q = get_paths(tmp_path / "x")  # explicit data dir (tests, embedding): environment ignored
    assert q.worktrees_dir == (tmp_path / "x-worktrees").resolve()


@pytest.mark.parametrize("wt", ["data/worktrees", "data", "."])
def test_nested_data_and_worktrees_dirs_are_rejected(tmp_path, wt):
    with pytest.raises(PathsConfigError):
        get_paths(tmp_path / "data", worktrees_dir=tmp_path / wt)


def test_nested_configuration_from_env_is_rejected_at_startup(monkeypatch, tmp_path):
    monkeypatch.setenv(config.ENV_DATA_DIR, str(tmp_path / "d"))
    monkeypatch.setenv(config.ENV_WORKTREES_DIR, str(tmp_path / "d" / "wt"))
    with pytest.raises(PathsConfigError):
        get_paths()
    with pytest.raises(PathsConfigError):
        Paths(tmp_path / "w" / "data", tmp_path / "w")  # data dir inside the worktrees dir


def test_ensure_creates_both_dirs(tmp_path):
    p = get_paths(tmp_path / "data").ensure(harden=False)
    assert p.data_dir.is_dir() and p.worktrees_dir.is_dir() and p.logs_dir.is_dir()


# --------------------------------------------------------------------------- C: run-path guard


def _dirs(tmp_path):
    data = tmp_path / "data"
    (data / "logs").mkdir(parents=True)
    return data


@pytest.mark.parametrize("rel", ["data", "data/logs", "data/../data", ".", "data/missing/deeper"])
def test_run_path_guard_refuses_cwd_overlapping_the_data_dir(tmp_path, rel):
    data = _dirs(tmp_path)
    with pytest.raises(InvalidRequestError):
        check_run_paths(str(tmp_path / rel), [], [data])


def test_run_path_guard_refuses_extra_dirs_overlapping_the_data_dir(tmp_path):
    data = _dirs(tmp_path)
    (tmp_path / "wt").mkdir()
    check_run_paths(str(tmp_path / "wt"), [str(tmp_path / "wt")], [data])  # fine
    for extra in (data, data / "logs", tmp_path):
        with pytest.raises(InvalidRequestError):
            check_run_paths(str(tmp_path / "wt"), [str(extra)], [data])


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows-only")
def test_run_path_guard_follows_junctions(tmp_path):
    import subprocess

    data = _dirs(tmp_path)
    link = tmp_path / "innocent"
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(data)], capture_output=True)
    if r.returncode != 0:
        pytest.skip("could not create a junction here")
    with pytest.raises(InvalidRequestError):
        check_run_paths(str(link), [], [data])


@pytest.mark.parametrize("provider", ["claude", "codex", "antigravity"])
async def test_execute_never_spawns_a_run_inside_the_data_dir(tmp_path, provider):
    data = _dirs(tmp_path)
    sp = FakeSpawner([])
    ad = ADAPTERS[provider](spawner=sp, env_allowlist=["PATH"], environ={}, resolver=fake_resolver, data_dir=data)
    req = ExecutionRequest(run_id="r", prompt="p", cwd=str(data / "logs"), permission="read_only")
    evs = [e async for e in ad.execute(req)]
    assert sp.calls == []  # nothing was started
    assert evs[-1].kind == K.RESULT and evs[-1].data["error_category"] == C.INVALID_REQUEST.value
    if provider == "codex":
        # The production activation refusal runs even before request preparation.
        # Independently preserve the protected-path contract instead of treating
        # the earlier refusal as evidence that path validation was exercised.
        assert "owned runtime activation" in evs[-1].data["error_message"]
        with pytest.raises(InvalidRequestError, match="data directory"):
            ad.check_request(req)
    else:
        assert "data directory" in evs[-1].data["error_message"]


def test_adapter_defaults_to_the_effective_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv(config.ENV_DATA_DIR, str(tmp_path / "dd"))
    ad = ADAPTERS["claude"](spawner=FakeSpawner([]), env_allowlist=["PATH"], environ={}, resolver=fake_resolver)
    assert ad.data_dir == (tmp_path / "dd").resolve()


def test_child_env_never_carries_orchestrator_variables():
    base = {"PATH": "C:\\bin", "ORCHESTRATOR_DATA_DIR": "C:\\d", "ORCHESTRATOR_TOKEN_FILE": "C:\\d\\api-token",
            "orchestrator_url": "http://x"}
    env = build_child_env(["PATH", "ORCHESTRATOR_DATA_DIR", "ORCHESTRATOR_TOKEN_FILE", "ORCHESTRATOR_URL"], base_env=base)
    assert set(env) == {"PATH", "AI_ORCHESTRATOR_DEPTH"}


def test_verifier_child_env_never_carries_orchestrator_variables(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_TOKEN_FILE", "C:\\d\\api-token")
    env = config.child_env(["PATH", "ORCHESTRATOR_TOKEN_FILE"])
    assert "ORCHESTRATOR_TOKEN_FILE" not in env and not any(k.upper().startswith("ORCHESTRATOR_") for k in env)


def test_default_allowlist_has_no_orchestrator_variables():
    from orchestrator.providers.process import load_env_allowlist

    assert not [n for n in load_env_allowlist() if n.upper().startswith("ORCHESTRATOR")]


# --------------------------------------------------------------------------- C: leader scratch cwd


async def test_leader_switch_and_advisor_scratch_is_outside_the_data_dir(tmp_path):
    import inspect

    from orchestrator.leader import advisor, objectives, switch

    for mod in (advisor, switch, objectives):
        src = inspect.getsource(mod)
        assert "leader-scratch" not in src and "str(self.ctx.paths.data_dir)" not in src, mod.__name__
    p = get_paths(tmp_path / "data")
    assert not _inside(p.leader_scratch_dir, p.data_dir)
    check_run_paths(str(p.leader_scratch_dir), [], p.protected_dirs)  # the guard accepts it


# --------------------------------------------------------------------------- YAML-only claude_shell


def test_claude_shell_override_from_settings_is_ignored():
    pol = effective_policies({"worker_permissions": {"claude_shell": True, "default": "workspace_write"}})
    assert pol.claude_shell is False
    assert pol.worker_permission("execute") == "workspace_write"


def test_claude_shell_yaml_value_is_used(tmp_path):
    (tmp_path / "policies.yaml").write_text("worker_permissions:\n  claude_shell: true\n", encoding="utf-8")
    assert effective_policies({}, tmp_path).claude_shell is True
    assert config.yaml_claude_shell(tmp_path) is True
    assert effective_policies({"worker_permissions": {"claude_shell": False}}, tmp_path).claude_shell is True


def test_shipped_policy_keeps_claude_shell_off():
    assert config.yaml_claude_shell() is False


def test_api_cannot_switch_the_claude_shell_on(tmp_path):
    app = create_app(data_dir=tmp_path / "data", providers={"mock": MockProvider()}, celebrate_seconds=0,
                     poll_interval=0.05)
    with TestClient(app, base_url=BASE) as c:
        h = {"Authorization": f"Bearer {app.state.rt.token}"}
        c.put("/api/settings", json={"values": {"worker_permissions": {"claude_shell": True}}}, headers=h)
        pol = c.portal.call(app.state.rt.ctx.policies)
        assert pol.claude_shell is False
